"""
Smart Planter API — Euregio Hackathon
-------------------------------------
One process that owns:
  * MQTT subscription of ESP32 telemetry      (planter/telemetry)
  * In-memory "last known state" + live SSE   (planter/state/*)
  * Watering rules + alert state machine      -> Telegram
  * SQLite: config, alert events, audit log
  * Home Assistant MQTT discovery
  * Read-only history proxy for Grafana/Influx

Design rule: the ESP32 holds the *safety* logic (pump max-on-time, default off).
This process holds the *comfort* logic (when to water, when to shout).
If this container dies, the plant still does not flood.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import hmac
import io
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

import httpx
import paho.mqtt.client as mqtt
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("planter")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def envf(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


DB_PATH = env("DB_PATH", "/data/planter.db")
SECRET_KEY = env("SECRET_KEY", "insecure-dev-key").encode()
ADMIN_USER = env("ADMIN_USER", "admin")
ADMIN_PASSWORD = env("ADMIN_PASSWORD", "planteradmin")

MQTT_HOST = env("MQTT_HOST", "mosquitto")
MQTT_PORT = int(env("MQTT_PORT", "1883"))
MQTT_USER = env("MQTT_USER", "planter")
MQTT_PASSWORD = env("MQTT_PASSWORD", "")

INFLUX_URL = env("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = env("INFLUX_TOKEN", "")
INFLUX_ORG = env("INFLUX_ORG", "kololab")
INFLUX_BUCKET = env("INFLUX_BUCKET", "planter")

TG_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = env("TELEGRAM_CHAT_ID", "")

TOPIC_TELEMETRY = env("TOPIC_TELEMETRY", "planter/telemetry")
TOPIC_CMD = env("TOPIC_CMD", "planter/cmd")
TOPIC_ESTOP = env("TOPIC_ESTOP", "planter/estop")
TOPIC_STATE = "planter/state"

# Tunables — every one of these is editable at runtime via PUT /api/config.
DEFAULTS: dict[str, Any] = {
    "pump_auto": True,
    "pump_threshold_pct": envf("PUMP_THRESHOLD_PCT", 30.0),   # water below this
    "pump_seconds": envf("PUMP_SECONDS", 20.0),               # run time per dose
    "pump_cooldown_min": envf("PUMP_COOLDOWN_MIN", 25.0),     # min gap between doses
    "pump_max_per_day": envf("PUMP_MAX_PER_DAY", 8.0),        # hard safety cap
    "alert_dry_pct": envf("ALERT_DRY_PCT", 25.0),             # alert if still dry
    "alert_dry_min": envf("ALERT_DRY_MIN", 20.0),             # ...for this long
    "alert_silent_min": envf("ALERT_SILENT_MIN", 5.0),        # node heartbeat lost
    "alert_tank_pct": envf("ALERT_TANK_PCT", 15.0),
    "light_on_below_lux": envf("LIGHT_ON_BELOW_LUX", 4000.0),
    "light_auto": False,
    "buzzer_enabled": True,
    "telegram_enabled": True,
}
NUMERIC_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, (int, float))}
BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}

# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #

_db_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def init_db() -> None:
    with _db_lock:
        c = db()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL, level TEXT NOT NULL, code TEXT NOT NULL,
                message TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                cleared_ts REAL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL, actor TEXT NOT NULL, ip TEXT,
                action TEXT NOT NULL, detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);
            """
        )
        now = time.time()
        for k, v in DEFAULTS.items():
            c.execute(
                "INSERT OR IGNORE INTO config(key,value,updated_at) VALUES(?,?,?)",
                (k, json.dumps(v), now),
            )
        # Drop keys that are no longer part of DEFAULTS (renamed/removed settings).
        placeholders = ",".join("?" * len(DEFAULTS))
        c.execute(f"DELETE FROM config WHERE key NOT IN ({placeholders})", tuple(DEFAULTS))
        c.commit()


def get_config() -> dict[str, Any]:
    with _db_lock:
        rows = db().execute("SELECT key,value FROM config").fetchall()
    cfg = dict(DEFAULTS)
    for r in rows:
        try:
            cfg[r["key"]] = json.loads(r["value"])
        except json.JSONDecodeError:
            pass
    return cfg


def set_config(updates: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for k, v in updates.items():
        if k not in DEFAULTS:
            continue
        if k in NUMERIC_KEYS:
            v = max(0.0, float(v))
        elif k in BOOL_KEYS:
            v = bool(v)
        clean[k] = v
    if clean:
        with _db_lock:
            c = db()
            for k, v in clean.items():
                c.execute(
                    "INSERT INTO config(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (k, json.dumps(v), time.time()),
                )
            c.commit()
    return clean


def audit(actor: str, action: str, detail: str = "", ip: str | None = None) -> None:
    with _db_lock:
        c = db()
        c.execute(
            "INSERT INTO audit(ts,actor,ip,action,detail) VALUES(?,?,?,?,?)",
            (time.time(), actor, ip, action, detail),
        )
        c.commit()


def open_event(level: str, code: str, message: str) -> bool:
    """Open an event if it is not already active. True if newly opened."""
    with _db_lock:
        c = db()
        row = c.execute(
            "SELECT id FROM events WHERE code=? AND active=1 ORDER BY id DESC LIMIT 1", (code,)
        ).fetchone()
        if row:
            return False
        c.execute(
            "INSERT INTO events(ts,level,code,message,active) VALUES(?,?,?,?,1)",
            (time.time(), level, code, message),
        )
        c.commit()
    return True


def clear_event(code: str) -> bool:
    with _db_lock:
        c = db()
        row = c.execute(
            "SELECT id FROM events WHERE code=? AND active=1 ORDER BY id DESC LIMIT 1", (code,)
        ).fetchone()
        if not row:
            return False
        c.execute(
            "UPDATE events SET active=0, cleared_ts=? WHERE id=?", (time.time(), row["id"])
        )
        c.commit()
    return True


def recent(table: str, limit: int) -> list[dict[str, Any]]:
    table = "events" if table == "events" else "audit"
    with _db_lock:
        rows = db().execute(
            f"SELECT * FROM {table} ORDER BY ts DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Runtime state
# --------------------------------------------------------------------------- #

STATE: dict[str, Any] = {
    "online": False,
    "last_seen": 0.0,
    "device": "smartplanter",
    "metrics": {},       # latest numeric readings
    "pump": "idle",      # idle | watering | cooldown
    "fault": None,
    "on_s": None,        # node uptime, used to spot restarts
    "restarts": 0,
    "last_restart_ts": 0.0,
    "alerts": [],        # active alert codes
    "last_pump_ts": 0.0,
    "pump_count_today": 0,
    "pump_day": datetime.now(timezone.utc).date().isoformat(),
    "halted": False,     # Not-Aus: forces every output off, blocks watering
}
HISTORY: deque[dict[str, Any]] = deque(maxlen=4000)   # fallback when Influx is down
SSE_CLIENTS: set[asyncio.Queue] = set()
LOOP: asyncio.AbstractEventLoop | None = None
MQTT_CONNECTED = False
MQTT_CLIENT: mqtt.Client | None = None

# Sensor keys we expect from the ESP32. Aliases map common variants onto one name.
ALIASES = {
    "moisture": "moisture_pct", "soil_moisture": "moisture_pct", "soil": "moisture_pct",
    "temperature": "temp_c", "temp": "temp_c",
    "hum": "humidity", "humidity_pct": "humidity",
    "light": "lux", "illuminance": "lux", "light_lux": "lux", "bh1750": "lux",
    "water_level": "tank_pct", "level": "tank_pct", "tank": "tank_pct",
    "wifi_rssi": "rssi",
}
PUBLISH_FIELDS = ["moisture_pct", "temp_c", "humidity", "lux", "tank_pct", "rssi", "pump", "mode", "fault", "on_s"]


def normalise(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_k, v in payload.items():
        k = ALIASES.get(raw_k.lower(), raw_k.lower())
        if isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:64]
    return out


def broadcast(event: dict[str, Any]) -> None:
    if LOOP is None:
        return
    for q in list(SSE_CLIENTS):
        try:
            LOOP.call_soon_threadsafe(q.put_nowait, event)
        except Exception:
            SSE_CLIENTS.discard(q)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

_last_tg: dict[str, float] = {}
TG_COOLDOWN = 600  # seconds — never spam the same alert


async def telegram(text: str, code: str = "generic", force: bool = False) -> None:
    if not TG_TOKEN or not TG_CHAT:
        return
    if not get_config().get("telegram_enabled", True):
        return
    now = time.time()
    if not force and now - _last_tg.get(code, 0) < TG_COOLDOWN:
        return
    _last_tg[code] = now
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            )
        log.info("telegram sent: %s", code)
    except Exception as exc:  # never let alerting take down the API
        log.warning("telegram failed: %s", exc)


async def raise_alert(code: str, level: str, message: str, tg_text: str) -> None:
    if open_event(level, code, message):
        log.warning("ALERT %s: %s", code, message)
        audit("system", "alert_raised", f"{code}: {message}")
        broadcast({"type": "alert", "code": code, "level": level, "message": message})
        await telegram(tg_text, code)


async def resolve_alert(code: str) -> None:
    if clear_event(code):
        log.info("ALERT CLEARED %s", code)
        audit("system", "alert_cleared", code)
        broadcast({"type": "alert_cleared", "code": code})


# --------------------------------------------------------------------------- #
# MQTT
# --------------------------------------------------------------------------- #


def ha_discovery_payloads(device: str) -> list[tuple[str, dict[str, Any]]]:
    """Home Assistant MQTT discovery — gives you a free HA device with history."""
    dev = {
        "identifiers": [f"planter_{device}"],
        "name": "Smart Planter",
        "model": "ESP32 planter node",
        "manufacturer": "Euregio Hackathon",
    }
    defs = [
        ("moisture_pct", "Bodenfeuchte", "%", "moisture", None),
        ("temp_c", "Temperatur", "°C", "temperature", None),
        ("humidity", "Luftfeuchte", "%", "humidity", None),
        ("lux", "Licht", "lx", "illuminance", None),
        ("tank_pct", "Wassertank", "%", None, None),
        ("rssi", "WLAN-Signal", "dBm", "signal_strength", "diagnostic"),
        ("pump", "Pumpe", None, None, None),
        ("last_seen", "Letztes Signal", None, "timestamp", "diagnostic"),
        ("uptime", "Laufzeit", "s", "duration", "diagnostic"),
    ]
    out: list[tuple[str, dict[str, Any]]] = []
    for key, name, unit, dclass, cat in defs:
        topic = f"homeassistant/sensor/planter_{device}/{key}/config"
        cfg: dict[str, Any] = {
            "unique_id": f"planter_{device}_{key}",
            "name": name,
            "state_topic": f"{TOPIC_STATE}/{key}",
            "device": dev,
            "availability_topic": f"{TOPIC_STATE}/online",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        if unit:
            cfg["unit_of_measurement"] = unit
            cfg["state_class"] = "measurement"
        if dclass:
            cfg["device_class"] = dclass
        if cat:
            cfg["entity_category"] = cat
        # binary-ish values arrive as strings; keep HA's parser happy
        if key in ("pump",):
            cfg["device_class"] = "running"
            cfg["payload_on"] = "watering"
            cfg["payload_off"] = "idle"
        out.append((topic, cfg))
    return out


def on_connect(client, userdata, flags, reason_code, properties=None):  # noqa: ANN001
    global MQTT_CONNECTED
    if reason_code == 0:
        MQTT_CONNECTED = True
        client.subscribe(TOPIC_TELEMETRY, qos=1)
        log.info("MQTT connected, subscribed to %s", TOPIC_TELEMETRY)
    else:
        MQTT_CONNECTED = False
        log.error("MQTT connect failed rc=%s", reason_code)


def on_disconnect(client, userdata, flags, reason_code, properties=None):  # noqa: ANN001
    global MQTT_CONNECTED
    MQTT_CONNECTED = False
    log.warning("MQTT disconnected rc=%s (auto-reconnect)", reason_code)


def on_message(client, userdata, msg):  # noqa: ANN001
    try:
        raw: dict[str, Any] = json.loads(msg.payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.warning("bad telemetry payload: %r", msg.payload[:120])
        return
    payload = normalise(raw)
    payload.setdefault("ts", time.time())
    STATE["metrics"].update({k: v for k, v in payload.items() if k not in ("ts", "device", "fw")})
    STATE["last_seen"] = time.time()
    STATE["online"] = True
    STATE["device"] = raw.get("device", STATE["device"])
    # A node that was running but now reports a small uptime has just restarted.
    # This is how we catch the classic pump-inrush brownout reset.
    on_s = payload.get("on_s")
    if isinstance(on_s, (int, float)):
        previous = STATE.get("on_s")
        if previous is not None and on_s < previous:
            STATE["restarts"] = STATE.get("restarts", 0) + 1
            STATE["last_restart_ts"] = time.time()
            log.warning("node restart detected (uptime %ss -> %ss)", previous, on_s)
            audit("system", "node_restart_detected", f"on_s {previous} -> {on_s}")
        STATE["on_s"] = on_s
    if "fault" in payload:
        STATE["fault"] = payload["fault"]
    HISTORY.append(payload)
    broadcast({"type": "telemetry", "data": payload})


def start_mqtt() -> None:
    global MQTT_CLIENT
    c = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"planter-api-{secrets.token_hex(3)}",
        clean_session=True,
    )
    if MQTT_USER:
        c.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    c.on_connect = on_connect
    c.on_disconnect = on_disconnect
    c.on_message = on_message
    c.reconnect_delay_set(min_delay=1, max_delay=30)
    MQTT_CLIENT = c
    while True:
        try:
            c.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            c.loop_start()
            return
        except Exception as exc:
            log.warning("MQTT not ready (%s), retrying in 5s", exc)
            time.sleep(5)


def publish(topic: str, payload: dict[str, Any], retain: bool = False) -> bool:
    if MQTT_CLIENT is None or not MQTT_CONNECTED:
        return False
    try:
        MQTT_CLIENT.publish(topic, json.dumps(payload), qos=1, retain=retain)
        return True
    except Exception as exc:
        log.warning("publish failed: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# Rules + state publishing
# --------------------------------------------------------------------------- #


def m(key: str) -> float | None:
    v = STATE["metrics"].get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def is_fresh() -> bool:
    """True if the last telemetry arrived recently enough to be trusted."""
    if not STATE["last_seen"]:
        return False
    return (time.time() - STATE["last_seen"]) < 30


async def rules_tick() -> None:
    """Runs every 5 s: watering decision + alert state machine."""
    cfg = get_config()
    now = time.time()
    last_seen = STATE["last_seen"]
    age = now - last_seen if last_seen else 1e9

    # -- reset the daily pump counter ------------------------------------- #
    today = datetime.now(timezone.utc).date().isoformat()
    if STATE["pump_day"] != today:
        STATE["pump_day"] = today
        STATE["pump_count_today"] = 0

    # -- node silence ------------------------------------------------------ #
    silent_min = cfg["alert_silent_min"]
    if last_seen and age > silent_min * 60:
        await raise_alert(
            "node_silent", "critical",
            f"Keine Daten seit {age/60:.0f} Minuten.",
            f"🚨 <b>Smart Planter</b>: keine Sensordaten seit {age/60:.0f} Minuten.\n"
            "Der ESP32 antwortet nicht. Bitte Strom und WLAN prüfen.",
        )
    elif last_seen and age < 90:
        await resolve_alert("node_silent")

    # -- watering decision ------------------------------------------------- #
    moisture = m("moisture_pct")
    tank = m("tank_pct")
    on_cooldown = (now - STATE["last_pump_ts"]) < cfg["pump_cooldown_min"] * 60
    daily_ok = STATE["pump_count_today"] < cfg["pump_max_per_day"]
    tank_ok = tank is None or tank > cfg["alert_tank_pct"]

    if STATE["pump"] == "watering":
        STATE["pump"] = "cooldown"

    if (
        cfg["pump_auto"]
        and not STATE["halted"]
        and moisture is not None
        and moisture < cfg["pump_threshold_pct"]
        and not on_cooldown
        and daily_ok
        and tank_ok
        and STATE["pump"] != "watering"
    ):
        secs = int(cfg["pump_seconds"])
        if publish(TOPIC_CMD, {"action": "pump", "seconds": secs, "reason": "auto"}):
            STATE["pump"] = "watering"
            STATE["last_pump_ts"] = now
            STATE["pump_count_today"] += 1
            log.info("pump ON for %ss (moisture %.1f%%)", secs, moisture)
            audit("system", "pump_auto", f"{secs}s, moisture={moisture:.1f}%")
            await telegram(
                f"💧 <b>Smart Planter</b> bewässert {secs}s "
                f"(Bodenfeuchte {moisture:.0f}%).",
                "pump_on", force=True,
            )
    elif STATE["pump"] == "cooldown" and not on_cooldown:
        STATE["pump"] = "idle"

    # -- dry soil too long ------------------------------------------------- #
    dry = moisture is not None and moisture < cfg["alert_dry_pct"]
    since = STATE.get("dry_since")
    if dry:
        STATE["dry_since"] = since or now
        if now - STATE["dry_since"] > cfg["alert_dry_min"] * 60:
            hint = " Tank leer?" if not tank_ok else ""
            await raise_alert(
                "soil_dry", "warning",
                f"Boden zu trocken ({moisture:.0f}%) seit "
                f"{cfg['alert_dry_min']:.0f} Minuten.{hint}",
                f"⚠️ <b>Smart Planter</b>: Boden zu trocken ({moisture:.0f}%) "
                f"seit {cfg['alert_dry_min']:.0f} Minuten.{hint}",
            )
    else:
        STATE["dry_since"] = None
        await resolve_alert("soil_dry")

    # -- empty tank -------------------------------------------------------- #
    if tank is not None and tank <= cfg["alert_tank_pct"]:
        await raise_alert(
            "tank_empty", "warning", f"Wassertank fast leer ({tank:.0f}%).",
            f"⚠️ <b>Smart Planter</b>: Wassertank fast leer ({tank:.0f}%). Bitte nachfüllen.",
        )
    elif tank is not None:
        await resolve_alert("tank_empty")

    # -- sensor fault reported by the node --------------------------------- #
    # "ok" is the healthy sentinel, not a fault — guard against truthiness.
    fault = STATE.get("fault")
    if fault and str(fault).lower() not in ("ok", "none", ""):
        await raise_alert(
            "sensor_fault", "warning", f"Sensorfehler: {fault}",
            f"⚠️ <b>Smart Planter</b>: Sensorfehler gemeldet: {fault}",
        )
    elif STATE["online"]:
        await resolve_alert("sensor_fault")

    # -- grow light (stretch) ---------------------------------------------- #
    lux = m("lux")
    if cfg["light_auto"] and lux is not None:
        want = "on" if lux < cfg["light_on_below_lux"] else "off"
        if STATE.get("_light_want") != want:
            publish(TOPIC_CMD, {"action": "light", "state": want, "reason": "auto"})
            STATE["_light_want"] = want

    STATE["alerts"] = [e["code"] for e in recent("events", 20) if e["active"]]


async def state_tick() -> None:
    """Publishes retained state for HA + dashboard every 5 s, plus HA discovery."""
    metrics = STATE["metrics"]
    for f in PUBLISH_FIELDS:
        if f == "pump":
            publish(f"{TOPIC_STATE}/pump", STATE["pump"], retain=True)
        elif f == "mode":
            publish(f"{TOPIC_STATE}/mode", "auto" if get_config()["pump_auto"] else "manual", retain=True)
        elif f == "fault":
            publish(f"{TOPIC_STATE}/fault", STATE.get("fault") or "ok", retain=True)
        elif f == "on_s":
            if STATE.get("on_s") is not None:
                publish(f"{TOPIC_STATE}/uptime", int(STATE["on_s"]), retain=True)
        elif f in metrics:
            publish(f"{TOPIC_STATE}/{f}", metrics[f], retain=True)
    publish(TOPIC_ESTOP, {"estop": STATE["halted"]}, retain=True)
    publish(f"{TOPIC_STATE}/last_seen", int(STATE["last_seen"] or time.time()), retain=True)
    publish(f"{TOPIC_STATE}/online", "online" if STATE["online"] else "offline", retain=True)
    for topic, cfg in ha_discovery_payloads(STATE["device"]):
        publish(topic, cfg, retain=True)


# --------------------------------------------------------------------------- #
# Influx history
# --------------------------------------------------------------------------- #

ALLOWED_FIELDS = {"moisture_pct", "temp_c", "humidity", "lux", "tank_pct", "rssi"}


async def influx_history(field: str, hours: float, every: str) -> list[dict[str, Any]]:
    if field not in ALLOWED_FIELDS or not INFLUX_TOKEN:
        raise ValueError("field not allowed or Influx not configured")
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{int(hours)}h)
  |> filter(fn: (r) => r._measurement == "planter")
  |> filter(fn: (r) => r._field == "{field}")
  |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_value"])
'''
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{INFLUX_URL}/api/v2/query",
            params={"org": INFLUX_ORG},
            headers={
                "Authorization": f"Token {INFLUX_TOKEN}",
                "Content-Type": "application/vnd.flux",
                "Accept": "application/csv",
            },
            content=flux,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"influx {r.status_code}: {r.text[:200]}")
    out: list[dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(r.text)):
        if not row or (row.get("#datatype") or "").startswith("string"):
            continue
        t, v = row.get("_time"), row.get("_value")
        if t and v not in (None, ""):
            try:
                out.append({"t": t, "v": float(v)})
            except ValueError:
                continue
    return out


def memory_history(field: str, hours: float) -> list[dict[str, Any]]:
    cutoff = time.time() - hours * 3600
    return [
        {"t": datetime.fromtimestamp(s["ts"], timezone.utc).isoformat(), "v": float(s[field])}
        for s in HISTORY
        if s.get("ts", 0) >= cutoff and isinstance(s.get(field), (int, float))
    ]


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

SESSION_TTL = 12 * 3600
_fail: dict[str, tuple[int, float]] = {}


def sign(payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(SECRET_KEY, body, sha256).digest()
    return (body + b"." + base64.urlsafe_b64encode(sig)).decode()


def verify(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    try:
        body_b64, sig_b64 = token.split(".", 1)
        body = body_b64.encode()
        want = hmac.new(SECRET_KEY, body, sha256).digest()
        if not hmac.compare_digest(want, base64.urlsafe_b64decode(sig_b64)):
            return None
        data = json.loads(base64.urlsafe_b64decode(body))
        if data.get("exp", 0) < time.time():
            return None
        return str(data.get("sub"))
    except Exception:
        return None


def require_user(request: Request, session: str | None) -> str:
    user = verify(session)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    return user


def client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", request.client.host if request.client else "?")


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    global LOOP
    LOOP = asyncio.get_running_loop()
    init_db()
    threading.Thread(target=start_mqtt, daemon=True).start()
    t1 = asyncio.create_task(_loop_task(rules_tick, 5))
    t2 = asyncio.create_task(_loop_task(state_tick, 5))
    log.info("Smart Planter API up")
    yield
    t1.cancel()
    t2.cancel()


async def _loop_task(fn, interval: float) -> None:
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("tick failed: %s", exc)
        await asyncio.sleep(interval)


app = FastAPI(title="Smart Planter API", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    age = time.time() - STATE["last_seen"] if STATE["last_seen"] else None
    return {
        "ok": True,
        "mqtt_connected": MQTT_CONNECTED,
        "node_online": is_fresh(),
        "last_seen_age_s": round(age, 1) if age is not None else None,
        "telegram": bool(TG_TOKEN and TG_CHAT),
    }


@app.get("/api/state")
async def api_state() -> dict[str, Any]:
    age = time.time() - STATE["last_seen"] if STATE["last_seen"] else None
    return {
        "metrics": STATE["metrics"],
        "pump": STATE["pump"],
        "fault": STATE.get("fault"),
        "alerts": STATE["alerts"],
        "halted": STATE["halted"],
        "restarts": STATE.get("restarts", 0),
        "on_s": STATE.get("on_s"),
        "online": is_fresh(),
        "last_seen": STATE["last_seen"],
        "last_seen_age_s": round(age, 1) if age is not None else None,
        "pump_count_today": STATE["pump_count_today"],
        "server_time": time.time(),
    }


@app.get("/api/config")
async def api_get_config() -> dict[str, Any]:
    return get_config()


@app.put("/api/config")
async def api_put_config(
    updates: dict[str, Any],
    request: Request,
    planter_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(request, planter_session)
    if len(updates) > 20:
        raise HTTPException(status_code=400, detail="too many keys")
    before = get_config()
    applied = set_config(updates)
    changes = {k: {"from": before.get(k), "to": v} for k, v in applied.items() if before.get(k) != v}
    if changes:
        audit(user, "config_changed", json.dumps(changes, ensure_ascii=False), client_ip(request))
    return {"ok": True, "applied": changes, "config": get_config()}


@app.post("/api/command")
async def api_command(
    body: dict[str, Any],
    request: Request,
    planter_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(request, planter_session)
    action = str(body.get("action", ""))
    if action not in ("pump", "light", "buzzer", "stop"):
        raise HTTPException(status_code=400, detail="unknown action")
    payload: dict[str, Any] = {"action": action, "reason": "manual", "by": user}
    if action == "pump":
        secs = int(max(1, min(float(body.get("seconds", get_config()["pump_seconds"])), 120)))
        payload["seconds"] = secs
        STATE["last_pump_ts"] = time.time()
        STATE["pump"] = "watering"
        STATE["pump_count_today"] += 1
    if action == "light":
        payload["state"] = "on" if str(body.get("state", "on")) == "on" else "off"
    if action == "buzzer" and not get_config().get("buzzer_enabled", True):
        raise HTTPException(status_code=409, detail="buzzer disabled in settings")
    if action == "buzzer":
        payload["seconds"] = int(max(1, min(float(body.get("seconds", 3)), 30)))
    if not publish(TOPIC_CMD, payload):
        raise HTTPException(status_code=503, detail="MQTT unavailable, command not sent")
    audit(user, f"manual_{action}", json.dumps(payload, ensure_ascii=False), client_ip(request))
    return {"ok": True, "sent": payload}


@app.get("/api/history")
async def api_history(field: str = "moisture_pct", hours: float = 6, every: str = "1m") -> dict[str, Any]:
    hours = max(0.1, min(hours, 24 * 30))
    if every not in ("10s", "1m", "5m", "15m", "1h"):
        every = "1m"
    try:
        points = await influx_history(field, hours, every)
        return {"source": "influxdb", "field": field, "points": points}
    except Exception as exc:
        log.warning("history fell back to memory: %s", exc)
        return {"source": "memory", "field": field, "points": memory_history(field, hours)}


@app.get("/api/events")
async def api_events(limit: int = 50, request: Request = None) -> list[dict[str, Any]]:  # type: ignore[assignment]
    return recent("events", limit)


@app.get("/api/audit")
async def api_audit(
    limit: int = 100,
    request: Request = None,  # type: ignore[assignment]
    planter_session: str | None = Cookie(default=None),
) -> list[dict[str, Any]]:
    require_user(request, planter_session)
    return recent("audit", limit)


@app.post("/api/login")
async def api_login(body: dict[str, Any], request: Request, response: Response) -> dict[str, Any]:
    ip = client_ip(request)
    count, until = _fail.get(ip, (0, 0.0))
    if until > time.time():
        raise HTTPException(status_code=429, detail=f"too many attempts, wait {int(until - time.time())}s")
    user = str(body.get("username", ""))
    pw = str(body.get("password", ""))
    ok_user = hmac.compare_digest(user, ADMIN_USER)
    ok_pw = hmac.compare_digest(pw, ADMIN_PASSWORD)
    if not (ok_user and ok_pw):
        _fail[ip] = (count + 1, time.time() + 300 if count + 1 >= 10 else 0.0)
        audit(user or "?", "login_failed", "", ip)
        raise HTTPException(status_code=401, detail="invalid credentials")
    _fail.pop(ip, None)
    token = sign({"sub": user, "exp": time.time() + SESSION_TTL})
    response.set_cookie(
        "planter_session", token, httponly=True, samesite="lax",
        max_age=SESSION_TTL, path="/",
    )
    audit(user, "login", "", ip)
    return {"ok": True, "user": user, "expires_in": SESSION_TTL}


@app.post("/api/logout")
async def api_logout(planter_session: str | None = Cookie(default=None), response: Response = None):  # type: ignore[assignment]
    user = verify(planter_session)
    if user:
        audit(user, "logout")
    response.delete_cookie("planter_session", path="/")
    return {"ok": True}


@app.get("/api/me")
async def api_me(planter_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = verify(planter_session)
    return {"authenticated": bool(user), "user": user, "read_only": not user}


@app.get("/api/stream")
async def api_stream(request: Request) -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    SSE_CLIENTS.add(q)

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'hello'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            SSE_CLIENTS.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/estop")
async def api_halt(
    request: Request,
    planter_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """Not-Aus. Sends the node into full stop and blocks automatic watering."""
    user = require_user(request, planter_session)
    STATE["halted"] = True
    STATE["pump"] = "idle"
    STATE.pop("dry_since", None)
    publish(TOPIC_CMD, {"action": "stop"})
    publish(TOPIC_ESTOP, {"estop": True}, retain=True)
    audit(user, "estop_all", "pump+light forced off, auto watering blocked", client_ip(request))
    await telegram(
        "🛑 <b>Smart Planter</b>: NOT-AUS gesetzt. Pumpe und Licht aus, "
        "automatische Bewässerung gesperrt.",
        "estop", force=True,
    )
    return {"ok": True, "halted": True}


@app.post("/api/resume")
async def api_resume(
    request: Request,
    planter_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(request, planter_session)
    STATE["halted"] = False
    publish(TOPIC_ESTOP, {"estop": False}, retain=True)
    audit(user, "estop_cleared", "automatic watering re-enabled", client_ip(request))
    return {"ok": True, "halted": False}


@app.get("/api/metrics")
async def api_metrics() -> Response:
    """Tiny Prometheus endpoint — so the existing kololab Prometheus can scrape the planter too."""
    lines: list[str] = []
    for k in PUBLISH_FIELDS:
        if k in STATE["metrics"] and isinstance(STATE["metrics"][k], (int, float)):
            lines.append(f"planter_{k} {float(STATE['metrics'][k])}")
    lines.append(f"planter_node_online {1 if is_fresh() else 0}")
    if STATE.get("on_s") is not None:
        lines.append(f"planter_node_uptime_seconds {STATE['on_s']}")
    lines.append(f"planter_node_restarts_total {STATE.get('restarts', 0)}")
    lines.append(f"planter_pump_watering {1 if STATE['pump'] == 'watering' else 0}")
    lines.append(f"planter_pump_count_today {STATE['pump_count_today']}")
    if STATE["last_seen"]:
        lines.append(f"planter_last_seen_timestamp {STATE['last_seen']}")
    body = "# HELP planter_* Smart Planter telemetry\n" + "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4")
