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

# Per-node topic scheme (multi-node safe):
#   planter/<node>/telemetry   node -> hub  (JSON, ~10 s)
#   planter/<node>/status      node -> hub  online/offline (LWT/birth)
#   planter/<node>/event       node -> hub  e.g. button_water_now
#   planter/<node>/cmd         hub -> node  {"action":...}
#   planter/<node>/estop       hub -> node  {"estop":bool} retained
#   planter/<node>/state/<f>   hub -> HA    retained per-field state
TOPIC_ROOT = env("TOPIC_ROOT", "planter")
TOPIC_TELEMETRY_WILDCARD = env("TOPIC_TELEMETRY_WILDCARD", f"{TOPIC_ROOT}/+/telemetry")
TOPIC_STATUS_WILDCARD = env("TOPIC_STATUS_WILDCARD", f"{TOPIC_ROOT}/+/status")
DISCOVERY_PREFIX = env("DISCOVERY_PREFIX", "homeassistant")


def t_cmd(node: str) -> str:
    return f"{TOPIC_ROOT}/{node}/cmd"


def t_estop(node: str) -> str:
    return f"{TOPIC_ROOT}/{node}/estop"


def t_event(node: str) -> str:
    return f"{TOPIC_ROOT}/{node}/event"


def t_state(node: str, field: str) -> str:
    return f"{TOPIC_ROOT}/{node}/state/{field}"

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
            CREATE TABLE IF NOT EXISTS plants (
                id TEXT PRIMARY KEY,
                node TEXT,
                name TEXT NOT NULL,
                species_id TEXT,
                species TEXT,
                emoji TEXT,
                color TEXT,
                nickname TEXT,
                notes TEXT,
                care TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT,
                kind TEXT,
                chip TEXT,
                mac TEXT,
                ip TEXT,
                port TEXT,
                notes TEXT,
                last_seen REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
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
# Plant database (species) + per-node plant profiles + devices
# --------------------------------------------------------------------------- #

PLANTS_DB_PATH = env("PLANTS_DB", os.path.join(os.path.dirname(__file__), "plants.json"))
_plant_db: dict[str, dict[str, Any]] = {}


def load_plant_db() -> dict[str, dict[str, Any]]:
    """Curated offline houseplant care data. Optionally refreshable from
    OpenPlantbook later; this ships with the image and needs no network."""
    global _plant_db
    if _plant_db:
        return _plant_db
    try:
        with open(PLANTS_DB_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        _plant_db = {p["id"]: p for p in data.get("plants", [])}
    except Exception as exc:  # noqa: BLE001
        log.warning("plant db load failed: %s", exc)
        _plant_db = {}
    return _plant_db


def _plant_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for k in ("care",):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, json.JSONDecodeError):
                d[k] = None
    return d


def get_plant(node_name: str) -> dict[str, Any] | None:
    with _db_lock:
        row = db().execute("SELECT * FROM plants WHERE node=?", (node_name,)).fetchone()
    return _plant_row(row) if row else None


def upsert_plant(node_name: str, data: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    species = None
    if data.get("species_id"):
        species = load_plant_db().get(str(data["species_id"]))
    care = data.get("care")
    if care is None and species:
        care = species.get("care")
    pid = str(data.get("id") or f"plant-{node_name}")
    fields = {
        "id": pid,
        "node": node_name,
        "name": str(data.get("name") or (species or {}).get("common_de")
                    or (species or {}).get("common") or node_name),
        "species_id": data.get("species_id"),
        "species": (species or {}).get("scientific") or data.get("species"),
        "emoji": data.get("emoji") or (species or {}).get("emoji") or "🪴",
        "color": data.get("color") or (species or {}).get("color") or "#5fd08a",
        "nickname": data.get("nickname"),
        "notes": data.get("notes") or (species or {}).get("notes"),
        "care": json.dumps(care) if care else None,
    }
    with _db_lock:
        db().execute(
            """INSERT INTO plants(id,node,name,species_id,species,emoji,color,nickname,notes,care,created_at,updated_at)
               VALUES(:id,:node,:name,:species_id,:species,:emoji,:color,:nickname,:notes,:care,:now,:now)
               ON CONFLICT(id) DO UPDATE SET
                 node=excluded.node, name=excluded.name, species_id=excluded.species_id,
                 species=excluded.species, emoji=excluded.emoji, color=excluded.color,
                 nickname=excluded.nickname, notes=excluded.notes, care=excluded.care,
                 updated_at=excluded.updated_at""",
            {**fields, "now": now},
        )
        db().commit()
    return get_plant(node_name) or {}


def list_plants() -> list[dict[str, Any]]:
    with _db_lock:
        rows = db().execute("SELECT * FROM plants ORDER BY name").fetchall()
    return [_plant_row(r) for r in rows]


def get_device(dev_id: str) -> dict[str, Any] | None:
    with _db_lock:
        row = db().execute("SELECT * FROM devices WHERE id=?", (dev_id,)).fetchone()
    return dict(row) if row else None


def upsert_device(dev_id: str, data: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    fields = {
        "id": dev_id,
        "name": data.get("name"),
        "kind": data.get("kind", "esp32"),
        "chip": data.get("chip"),
        "mac": data.get("mac"),
        "ip": data.get("ip"),
        "port": data.get("port"),
        "notes": data.get("notes"),
        "last_seen": data.get("last_seen"),
    }
    with _db_lock:
        db().execute(
            """INSERT INTO devices(id,name,kind,chip,mac,ip,port,notes,last_seen,created_at,updated_at)
               VALUES(:id,:name,:kind,:chip,:mac,:ip,:port,:notes,:last_seen,:now,:now)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, kind=excluded.kind, chip=excluded.chip, mac=excluded.mac,
                 ip=excluded.ip, port=excluded.port, notes=excluded.notes,
                 last_seen=excluded.last_seen, updated_at=excluded.updated_at""",
            {**fields, "now": now},
        )
        db().commit()
    return get_device(dev_id) or {}


def list_devices() -> list[dict[str, Any]]:
    with _db_lock:
        rows = db().execute("SELECT * FROM devices ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Runtime state
# --------------------------------------------------------------------------- #

def new_node(name: str) -> dict[str, Any]:
    return {
        "device": name,
        "online": False,
        "last_seen": 0.0,
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


NODES: dict[str, dict[str, Any]] = {}
NODES_LOCK = threading.Lock()
HISTORY: dict[str, deque[dict[str, Any]]] = {}      # per-node fallback when Influx is down
SSE_CLIENTS: set[asyncio.Queue] = set()
LOOP: asyncio.AbstractEventLoop | None = None
MQTT_CONNECTED = False
MQTT_CLIENT: mqtt.Client | None = None


def node(name: str) -> dict[str, Any]:
    """Get or create the runtime state for one node."""
    with NODES_LOCK:
        n = NODES.get(name)
        if n is None:
            n = new_node(name)
            NODES[name] = n
        return n


def node_names() -> list[str]:
    with NODES_LOCK:
        return sorted(NODES.keys())


def node_history(name: str) -> deque[dict[str, Any]]:
    with NODES_LOCK:
        h = HISTORY.get(name)
        if h is None:
            h = deque(maxlen=4000)
            HISTORY[name] = h
        return h

# Sensor keys we expect from the ESP32. Aliases map common variants onto one name.
ALIASES = {
    "moisture": "moisture_pct", "soil_moisture": "moisture_pct", "soil": "moisture_pct",
    "temperature": "temp_c", "temp": "temp_c",
    "hum": "humidity", "humidity_pct": "humidity",
    "light": "lux", "illuminance": "lux", "light_lux": "lux", "bh1750": "lux",
    "water_level": "tank_pct", "level": "tank_pct", "tank": "tank_pct",
    "wifi_rssi": "rssi",
}
PUBLISH_FIELDS = ["moisture_pct", "temp_c", "humidity", "lux", "soil_v", "tank_pct",
                  "rssi", "pump", "light", "mode", "fault", "on_s"]


def normalise(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_k, v in payload.items():
        raw = raw_k.lower()
        k = ALIASES.get(raw, raw)
        # Never let an alias clobber a key that was sent explicitly. The
        # firmware publishes both `lux` (light level) and `light` (relay state);
        # without this guard the alias light->lux would overwrite the real lux
        # with "on"/"off".
        if raw in ALIASES and k in out:
            continue
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


def ha_discovery_payloads(node_name: str, friendly: str | None = None,
                          model: str = "ESP32 planter node") -> list[tuple[str, dict[str, Any]]]:
    """Home Assistant MQTT discovery — one HA device per plant node, with sensors
    plus control entities (water button, buzzer button, grow-light switch)."""
    dev = {
        "identifiers": [f"planter_{node_name}"],
        "name": friendly or node_name,
        "model": model,
        "manufacturer": "FloraHome",
    }
    sensors = [
        ("moisture_pct", "Bodenfeuchte", "%", "moisture", None),
        ("temp_c", "Temperatur", "°C", "temperature", None),
        ("humidity", "Luftfeuchte", "%", "humidity", None),
        ("lux", "Licht", "lx", "illuminance", None),
        ("tank_pct", "Wassertank", "%", None, None),
        ("soil_v", "Boden Rohspannung", "V", "voltage", "diagnostic"),
        ("rssi", "WLAN-Signal", "dBm", "signal_strength", "diagnostic"),
        ("pump", "Pumpe", None, "running", None),
        ("last_seen", "Letztes Signal", None, "timestamp", "diagnostic"),
        ("uptime", "Laufzeit", "s", "duration", "diagnostic"),
    ]
    out: list[tuple[str, dict[str, Any]]] = []
    for key, name, unit, dclass, cat in sensors:
        topic = f"{DISCOVERY_PREFIX}/sensor/planter_{node_name}/{key}/config"
        cfg: dict[str, Any] = {
            "unique_id": f"planter_{node_name}_{key}",
            "name": name,
            "state_topic": t_state(node_name, key),
            "device": dev,
            "availability_topic": t_state(node_name, "online"),
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
        if key == "pump":
            cfg["payload_on"] = "watering"
            cfg["payload_off"] = "idle"
        out.append((topic, cfg))

    # --- control entities -------------------------------------------------- #
    def cmd_button(object_id: str, name: str, payload: str, icon: str) -> None:
        out.append((
            f"{DISCOVERY_PREFIX}/button/planter_{node_name}/{object_id}/config",
            {
                "unique_id": f"planter_{node_name}_{object_id}",
                "name": name,
                "command_topic": t_cmd(node_name),
                "payload_press": payload,
                "device": dev,
                "availability_topic": t_state(node_name, "online"),
                "icon": icon,
            },
        ))

    cmd_button("water", "Jetzt bewässern", json.dumps({"action": "pump", "seconds": 20, "reason": "ha"}), "mdi:watering-can")
    cmd_button("buzzer", "Buzzer testen", json.dumps({"action": "buzzer", "seconds": 3}), "mdi:bullhorn")

    out.append((
        f"{DISCOVERY_PREFIX}/switch/planter_{node_name}/light/config",
        {
            "unique_id": f"planter_{node_name}_light",
            "name": "Grow Light",
            "state_topic": t_state(node_name, "light"),
            "command_topic": t_cmd(node_name),
            "payload_on": json.dumps({"action": "light", "state": "on"}),
            "payload_off": json.dumps({"action": "light", "state": "off"}),
            "state_on": "on",
            "state_off": "off",
            "device": dev,
            "availability_topic": t_state(node_name, "online"),
            "icon": "mdi:lightbulb",
        },
    ))
    return out


def on_connect(client, userdata, flags, reason_code, properties=None):  # noqa: ANN001
    global MQTT_CONNECTED
    if reason_code == 0:
        MQTT_CONNECTED = True
        client.subscribe(TOPIC_TELEMETRY_WILDCARD, qos=1)
        client.subscribe(TOPIC_STATUS_WILDCARD, qos=1)
        log.info("MQTT connected, subscribed to %s + %s",
                 TOPIC_TELEMETRY_WILDCARD, TOPIC_STATUS_WILDCARD)
    else:
        MQTT_CONNECTED = False
        log.error("MQTT connect failed rc=%s", reason_code)


def on_disconnect(client, userdata, flags, reason_code, properties=None):  # noqa: ANN001
    global MQTT_CONNECTED
    MQTT_CONNECTED = False
    log.warning("MQTT disconnected rc=%s (auto-reconnect)", reason_code)


def parse_topic(topic: str) -> tuple[str, str] | None:
    """planter/<node>/<kind> -> (node, kind); None for anything else."""
    parts = topic.split("/")
    if len(parts) >= 3 and parts[0] == TOPIC_ROOT:
        return parts[1], parts[2]
    return None


def on_message(client, userdata, msg):  # noqa: ANN001
    parsed = parse_topic(msg.topic)
    if not parsed:
        return
    node_name, kind = parsed

    if kind == "status":
        online = msg.payload.decode(errors="replace").strip().lower() == "online"
        with NODES_LOCK:
            exists = node_name in NODES
        if not exists and not online:
            return  # stale retained "offline" for a node we have never seen
        n = node(node_name)
        n["online"] = online
        if online:
            n["last_seen"] = time.time()
        broadcast({"type": "status", "node": node_name, "online": online})
        return

    if kind != "telemetry":
        return

    n = node(node_name)

    try:
        raw: dict[str, Any] = json.loads(msg.payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.warning("bad telemetry payload on %s: %r", msg.topic, msg.payload[:120])
        return
    payload = normalise(raw)
    payload.setdefault("ts", time.time())
    payload["device"] = raw.get("device", node_name)
    n["metrics"].update({k: v for k, v in payload.items() if k not in ("ts", "device", "fw")})
    n["last_seen"] = time.time()
    n["online"] = True
    n["device"] = payload["device"]
    # A node that was running but now reports a small uptime has just restarted.
    # This is how we catch the classic pump-inrush brownout reset.
    on_s = payload.get("on_s")
    if isinstance(on_s, (int, float)):
        previous = n.get("on_s")
        if previous is not None and on_s < previous:
            n["restarts"] = n.get("restarts", 0) + 1
            n["last_restart_ts"] = time.time()
            log.warning("[%s] node restart detected (uptime %ss -> %ss)", node_name, previous, on_s)
            audit("system", "node_restart_detected", f"{node_name}: on_s {previous} -> {on_s}")
        n["on_s"] = on_s
    if "fault" in payload:
        n["fault"] = payload["fault"]
    node_history(node_name).append(payload)
    broadcast({"type": "telemetry", "node": node_name, "data": payload})


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


def m(n: dict[str, Any], key: str) -> float | None:
    v = n["metrics"].get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def node_fresh(n: dict[str, Any]) -> bool:
    """True if this node's last telemetry arrived recently enough to trust."""
    if not n["last_seen"]:
        return False
    return (time.time() - n["last_seen"]) < 30


def is_fresh() -> bool:
    """True if at least one node is fresh."""
    return any(node_fresh(NODES[k]) for k in node_names())


async def _rules_for_node(node_name: str, n: dict[str, Any]) -> None:
    cfg = get_config()
    now = time.time()
    last_seen = n["last_seen"]
    age = now - last_seen if last_seen else 1e9
    label = n.get("device", node_name)

    # -- reset the daily pump counter ------------------------------------- #
    today = datetime.now(timezone.utc).date().isoformat()
    if n["pump_day"] != today:
        n["pump_day"] = today
        n["pump_count_today"] = 0

    # -- node silence ------------------------------------------------------ #
    silent_min = cfg["alert_silent_min"]
    if last_seen and age > silent_min * 60:
        await raise_alert(
            f"{node_name}:node_silent", "critical",
            f"[{label}] Keine Daten seit {age/60:.0f} Minuten.",
            f"🚨 <b>{label}</b>: keine Sensordaten seit {age/60:.0f} Minuten. "
            "Bitte Strom und WLAN prüfen.",
        )
    elif last_seen and age < 90:
        await resolve_alert(f"{node_name}:node_silent")

    # -- watering decision ------------------------------------------------- #
    moisture = m(n, "moisture_pct")
    tank = m(n, "tank_pct")
    on_cooldown = (now - n["last_pump_ts"]) < cfg["pump_cooldown_min"] * 60
    daily_ok = n["pump_count_today"] < cfg["pump_max_per_day"]
    tank_ok = tank is None or tank > cfg["alert_tank_pct"]

    if n["pump"] == "watering":
        n["pump"] = "cooldown"

    if (
        cfg["pump_auto"]
        and not n["halted"]
        and moisture is not None
        and moisture < cfg["pump_threshold_pct"]
        and not on_cooldown
        and daily_ok
        and tank_ok
        and n["pump"] != "watering"
    ):
        secs = int(cfg["pump_seconds"])
        if publish(t_cmd(node_name), {"action": "pump", "seconds": secs, "reason": "auto"}):
            n["pump"] = "watering"
            n["last_pump_ts"] = now
            n["pump_count_today"] += 1
            log.info("[%s] pump ON for %ss (moisture %.1f%%)", node_name, secs, moisture)
            audit("system", "pump_auto", f"{node_name}: {secs}s, moisture={moisture:.1f}%")
            await telegram(
                f"💧 <b>{label}</b> bewässert {secs}s (Bodenfeuchte {moisture:.0f}%).",
                f"{node_name}_pump_on", force=True,
            )
    elif n["pump"] == "cooldown" and not on_cooldown:
        n["pump"] = "idle"

    # -- dry soil too long ------------------------------------------------- #
    dry = moisture is not None and moisture < cfg["alert_dry_pct"]
    since = n.get("dry_since")
    if dry:
        n["dry_since"] = since or now
        if now - n["dry_since"] > cfg["alert_dry_min"] * 60:
            hint = " Tank leer?" if not tank_ok else ""
            await raise_alert(
                f"{node_name}:soil_dry", "warning",
                f"[{label}] Boden zu trocken ({moisture:.0f}%) seit "
                f"{cfg['alert_dry_min']:.0f} Minuten.{hint}",
                f"⚠️ <b>{label}</b>: Boden zu trocken ({moisture:.0f}%) "
                f"seit {cfg['alert_dry_min']:.0f} Minuten.{hint}",
            )
    else:
        n["dry_since"] = None
        await resolve_alert(f"{node_name}:soil_dry")

    # -- empty tank -------------------------------------------------------- #
    if tank is not None and tank <= cfg["alert_tank_pct"]:
        await raise_alert(
            f"{node_name}:tank_empty", "warning", f"[{label}] Wassertank fast leer ({tank:.0f}%).",
            f"⚠️ <b>{label}</b>: Wassertank fast leer ({tank:.0f}%). Bitte nachfüllen.",
        )
    elif tank is not None:
        await resolve_alert(f"{node_name}:tank_empty")

    # -- sensor fault reported by the node --------------------------------- #
    # "ok" is the healthy sentinel, not a fault — guard against truthiness.
    fault = n.get("fault")
    if fault and str(fault).lower() not in ("ok", "none", ""):
        await raise_alert(
            f"{node_name}:sensor_fault", "warning", f"[{label}] Sensorfehler: {fault}",
            f"⚠️ <b>{label}</b>: Sensorfehler gemeldet: {fault}",
        )
    elif n["online"]:
        await resolve_alert(f"{node_name}:sensor_fault")

    # -- grow light (stretch) ---------------------------------------------- #
    lux = m(n, "lux")
    if cfg["light_auto"] and lux is not None:
        want = "on" if lux < cfg["light_on_below_lux"] else "off"
        if n.get("_light_want") != want:
            publish(t_cmd(node_name), {"action": "light", "state": want, "reason": "auto"})
            n["_light_want"] = want

    n["alerts"] = [e["code"].split(":", 1)[-1]
                   for e in recent("events", 60)
                   if e["active"] and e["code"].startswith(f"{node_name}:")]


async def rules_tick() -> None:
    """Runs every 5 s: per-node watering decision + alert state machine."""
    for name in node_names():
        await _rules_for_node(name, node(name))


async def _state_for_node(node_name: str, n: dict[str, Any]) -> None:
    metrics = n["metrics"]
    for f in PUBLISH_FIELDS:
        if f == "pump":
            publish(t_state(node_name, "pump"), n["pump"], retain=True)
        elif f == "mode":
            publish(t_state(node_name, "mode"), "auto" if get_config()["pump_auto"] else "manual", retain=True)
        elif f == "fault":
            publish(t_state(node_name, "fault"), n.get("fault") or "ok", retain=True)
        elif f == "on_s":
            if n.get("on_s") is not None:
                publish(t_state(node_name, "uptime"), int(n["on_s"]), retain=True)
        elif f == "light":
            publish(t_state(node_name, "light"), metrics.get("light") or "off", retain=True)
        elif f in metrics:
            publish(t_state(node_name, f), metrics[f], retain=True)
    publish(t_estop(node_name), {"estop": n["halted"]}, retain=True)
    publish(t_state(node_name, "last_seen"), int(n["last_seen"] or time.time()), retain=True)
    publish(t_state(node_name, "online"), "online" if node_fresh(n) else "offline", retain=True)
    for topic, cfg in ha_discovery_payloads(node_name, n.get("device")):
        publish(topic, cfg, retain=True)


async def state_tick() -> None:
    """Publishes per-node retained state for HA + dashboard every 5 s, plus discovery."""
    for name in node_names():
        await _state_for_node(name, node(name))


# --------------------------------------------------------------------------- #
# Influx history
# --------------------------------------------------------------------------- #

ALLOWED_FIELDS = {"moisture_pct", "temp_c", "humidity", "lux", "tank_pct", "rssi"}


async def influx_history(field: str, hours: float, every: str, node_name: str | None = None) -> list[dict[str, Any]]:
    if field not in ALLOWED_FIELDS or not INFLUX_TOKEN:
        raise ValueError("field not allowed or Influx not configured")
    node_filter = f'\n  |> filter(fn: (r) => r.device == "{node_name}")' if node_name else ""
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{int(hours)}h)
  |> filter(fn: (r) => r._measurement == "planter"){node_filter}
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


def memory_history(field: str, hours: float, node_name: str | None = None) -> list[dict[str, Any]]:
    cutoff = time.time() - hours * 3600
    src = node_history(node_name) if node_name else [p for k in node_names() for p in node_history(k)]
    return [
        {"t": datetime.fromtimestamp(s["ts"], timezone.utc).isoformat(), "v": float(s[field])}
        for s in src
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


def _primary_name() -> str:
    names = node_names()
    for k in names:
        if node_fresh(NODES[k]):
            return k
    return names[0] if names else "plant-a"


def public_node(name: str, n: dict[str, Any]) -> dict[str, Any]:
    age = time.time() - n["last_seen"] if n["last_seen"] else None
    return {
        "node": name,
        "device": n.get("device", name),
        "metrics": n["metrics"],
        "pump": n["pump"],
        "fault": n.get("fault"),
        "alerts": n["alerts"],
        "halted": n["halted"],
        "restarts": n.get("restarts", 0),
        "on_s": n.get("on_s"),
        "online": node_fresh(n),
        "last_seen": n["last_seen"],
        "last_seen_age_s": round(age, 1) if age is not None else None,
        "pump_count_today": n["pump_count_today"],
    }


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
    names = node_names()
    return {
        "ok": True,
        "mqtt_connected": MQTT_CONNECTED,
        "node_online": is_fresh(),
        "nodes": {k: node_fresh(NODES[k]) for k in names},
        "node_count": len(names),
        "telegram": bool(TG_TOKEN and TG_CHAT),
    }


@app.get("/api/nodes")
@app.get("/api/state")
async def api_state() -> dict[str, Any]:
    """All nodes keyed by node name, plus a `primary` convenience node."""
    names = node_names()
    nodes = {k: public_node(k, NODES[k]) for k in names}
    for k in names:
        nodes[k]["plant"] = get_plant(k)
    return {
        "nodes": nodes,
        "count": len(names),
        "primary": nodes.get(_primary_name()) if names else None,
        "server_time": time.time(),
    }


# ---- plant species database ------------------------------------------------- #

@app.get("/api/plants")
async def api_plants(q: str | None = None) -> dict[str, Any]:
    """Species catalog (offline curated data). `?q=` filters by name."""
    items = list(load_plant_db().values())
    if q:
        ql = q.lower()
        items = [p for p in items if ql in p["common"].lower()
                 or ql in (p.get("common_de") or "").lower()
                 or ql in (p.get("scientific") or "").lower()]
    return {"count": len(items), "plants": items}


@app.get("/api/plants/{species_id}")
async def api_plant_species(species_id: str) -> dict[str, Any]:
    p = load_plant_db().get(species_id)
    if not p:
        raise HTTPException(status_code=404, detail="unknown species")
    return p


# ---- per-node plant profiles ------------------------------------------------ #

@app.get("/api/plant/{node}")
async def api_get_plant(node: str) -> dict[str, Any]:
    return {"node": node, "plant": get_plant(node)}


@app.put("/api/plant/{node}")
async def api_put_plant(
    node: str,
    data: dict[str, Any],
    request: Request,
    planter_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(request, planter_session)
    plant = upsert_plant(node, data)
    audit(user, "plant_profile_saved", f"{node}: {plant.get('name')}", client_ip(request))
    return {"ok": True, "plant": plant}


@app.get("/api/plants/assigned/all")
async def api_plants_assigned() -> dict[str, Any]:
    return {"plants": list_plants()}


# ---- devices + flashing (dashboard) ----------------------------------------- #

FLASHER_URL = env("FLASHER_URL", "http://127.0.0.1:6053")


async def _flasher_get(path: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"{FLASHER_URL}{path}")
            return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@app.get("/api/devices")
async def api_devices() -> dict[str, Any]:
    """Attached ESPs + known devices. Merges the flasher service's view."""
    live = await _flasher_get("/devices")
    known = {d["id"]: d for d in list_devices()}
    for dev in live.get("devices", []):
        mac = dev.get("mac")
        dev_id = mac or dev.get("port")
        if dev_id and dev_id not in known:
            upsert_device(dev_id, {"kind": "esp32", "port": dev.get("port"),
                                   "chip": dev.get("chip"), "mac": mac, "last_seen": time.time()})
    return {"attached": live.get("devices", []), "known": list_devices(),
            "flasher": live.get("error") if live.get("error") else "ok"}


@app.get("/api/nodes/available")
async def api_nodes_available() -> dict[str, Any]:
    """Node configs available to flash."""
    return await _flasher_get("/nodes")


@app.post("/api/flash")
async def api_flash(
    body: dict[str, Any],
    request: Request,
    planter_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """Flash a node over USB. The flasher does the esptool/esphome work."""
    user = require_user(request, planter_session)
    node = str(body.get("node", ""))
    port = str(body.get("port", "/dev/ttyUSB0"))
    if not node:
        raise HTTPException(status_code=400, detail="node required")
    audit(user, "flash_requested", f"{node} on {port}", client_ip(request))
    try:
        async with httpx.AsyncClient(timeout=900) as client:
            r = await client.post(f"{FLASHER_URL}/flash", json={"node": node, "port": port})
            result = r.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"flasher unavailable: {exc}")
    audit(user, "flash_done" if result.get("ok") else "flash_failed",
          f"{node} on {port}", client_ip(request))
    return result


# ---- hotspot / Wi-Fi settings (needs host access) --------------------------- #

@app.get("/api/hotspot")
async def api_hotspot(request: Request, planter_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    require_user(request, planter_session)
    return {
        "note": "Hotspot (FloraHome) verwaltet der Host: nmcli con show Hotspot.",
        "ssid": "FloraHome",
        "owner": "host",
    }


@app.put("/api/hotspot")
async def api_hotspot_put(
    data: dict[str, Any],
    request: Request,
    planter_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(request, planter_session)
    ssid = data.get("ssid")
    # The API container has no host namespace; this is intentionally a spool.
    audit(user, "hotspot_change_requested", json.dumps(data, ensure_ascii=False), client_ip(request))
    return {"ok": False,
            "detail": "Hotspot-Änderung wird gespoolt (siehe docs/DEVICES.md). "
                      "Aktuell per Host: nmcli con mod Hotspot 802-11-wireless.ssid <name>",
            "requested": {"ssid": ssid}}


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
    node_name = str(body.get("node") or _primary_name())
    n = node(node_name)
    action = str(body.get("action", ""))
    if action not in ("pump", "light", "buzzer", "stop", "cal"):
        raise HTTPException(status_code=400, detail="unknown action")
    payload: dict[str, Any] = {"action": action, "reason": "manual", "by": user}
    if action == "pump":
        secs = int(max(1, min(float(body.get("seconds", get_config()["pump_seconds"])), 120)))
        payload["seconds"] = secs
        n["last_pump_ts"] = time.time()
        n["pump"] = "watering"
        n["pump_count_today"] += 1
    if action == "light":
        want = str(body.get("state", "on"))
        if want == "toggle":
            current = str((n.get("metrics") or {}).get("light") or "off")
            want = "off" if current == "on" else "on"
        payload["state"] = "on" if want == "on" else "off"
    if action == "buzzer" and not get_config().get("buzzer_enabled", True):
        raise HTTPException(status_code=409, detail="buzzer disabled in settings")
    if action == "buzzer":
        payload["seconds"] = int(max(1, min(float(body.get("seconds", 3)), 30)))
    if action == "cal":
        # runtime soil calibration, persisted on the node (no reflash)
        try:
            dry = float(body.get("soil_dry_v"))
            wet = float(body.get("soil_wet_v"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="soil_dry_v and soil_wet_v (volts) required")
        if not (0.0 <= dry <= 3.6 and 0.0 <= wet <= 3.6):
            raise HTTPException(status_code=400, detail="voltages must be between 0 and 3.6 V")
        if dry <= wet:
            raise HTTPException(status_code=400, detail="dry voltage must exceed wet voltage")
        payload["soil_dry_v"] = round(dry, 3)
        payload["soil_wet_v"] = round(wet, 3)
    payload["node"] = node_name
    if not publish(t_cmd(node_name), payload):
        raise HTTPException(status_code=503, detail="MQTT unavailable, command not sent")
    audit(user, f"manual_{action}", json.dumps(payload, ensure_ascii=False), client_ip(request))
    return {"ok": True, "node": node_name, "sent": payload}


@app.get("/api/history")
async def api_history(field: str = "moisture_pct", hours: float = 6, every: str = "1m",
                      node: str | None = None) -> dict[str, Any]:
    hours = max(0.1, min(hours, 24 * 30))
    if every not in ("10s", "1m", "5m", "15m", "1h"):
        every = "1m"
    node_name = node or _primary_name()
    try:
        points = await influx_history(field, hours, every, node_name)
        return {"source": "influxdb", "field": field, "node": node_name, "points": points}
    except Exception as exc:
        log.warning("history fell back to memory: %s", exc)
        return {"source": "memory", "field": field, "node": node_name,
                "points": memory_history(field, hours, node_name)}


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
    body: dict[str, Any] | None = None,
    planter_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """Not-Aus. Stops the node(s) and blocks automatic watering. Body may carry
    {"node": "<name>"} to target one node; omitted = every node."""
    user = require_user(request, planter_session)
    targets = [(body or {}).get("node")] if (body or {}).get("node") else node_names()
    for name in targets:
        n = node(name)
        n["halted"] = True
        n["pump"] = "idle"
        n.pop("dry_since", None)
        publish(t_cmd(name), {"action": "stop"})
        publish(t_estop(name), {"estop": True}, retain=True)
    audit(user, "estop_all", f"nodes={targets} pump+light forced off, auto watering blocked",
          client_ip(request))
    await telegram(
        "🛑 <b>FloraHome</b>: NOT-AUS gesetzt. Pumpe und Licht aus, "
        "automatische Bewässerung gesperrt.",
        "estop", force=True,
    )
    return {"ok": True, "halted": True, "nodes": targets}


@app.post("/api/resume")
async def api_resume(
    request: Request,
    body: dict[str, Any] | None = None,
    planter_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(request, planter_session)
    targets = [(body or {}).get("node")] if (body or {}).get("node") else node_names()
    for name in targets:
        node(name)["halted"] = False
        publish(t_estop(name), {"estop": False}, retain=True)
    audit(user, "estop_cleared", f"nodes={targets} automatic watering re-enabled", client_ip(request))
    return {"ok": True, "halted": False, "nodes": targets}


@app.get("/api/metrics")
async def api_metrics() -> Response:
    """Prometheus endpoint — per-node, with a `node` label."""
    lines: list[str] = ["# HELP planter_* FloraHome telemetry"]
    for name in node_names():
        n = node(name)
        m = n["metrics"]
        for k in PUBLISH_FIELDS:
            if k in m and isinstance(m[k], (int, float)):
                lines.append(f'planter_{k}{{node="{name}"}} {float(m[k])}')
        lines.append(f'planter_node_online{{node="{name}"}} {1 if node_fresh(n) else 0}')
        if n.get("on_s") is not None:
            lines.append(f'planter_node_uptime_seconds{{node="{name}"}} {int(n["on_s"])}')
        lines.append(f'planter_node_restarts_total{{node="{name}"}} {n.get("restarts", 0)}')
        lines.append(f'planter_pump_watering{{node="{name}"}} {1 if n["pump"] == "watering" else 0}')
        lines.append(f'planter_pump_count_today{{node="{name}"}} {n["pump_count_today"]}')
        if n["last_seen"]:
            lines.append(f'planter_last_seen_timestamp{{node="{name}"}} {n["last_seen"]}')
    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4")
