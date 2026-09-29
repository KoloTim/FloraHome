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
import platform
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
START_TS = time.time()

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
TOPIC_EVENT_WILDCARD = env("TOPIC_EVENT_WILDCARD", f"{TOPIC_ROOT}/+/event")
DISCOVERY_PREFIX = env("DISCOVERY_PREFIX", "homeassistant")

# How long without a message before a node counts as offline. This must be safely
# larger than the firmware publish interval (10 s): the old hardcoded 30 s meant a
# couple of missed cycles, a Wi-Fi roam, an AP rekey or a broker restart flipped a
# perfectly healthy node to "offline". 90 s (9 missed heartbeats) is forgiving but
# still fast enough to be useful. Editable at runtime via config `node_offline_sec`.
NODE_OFFLINE_SEC = envf("NODE_OFFLINE_SEC", 90.0)


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
    "node_offline_sec": NODE_OFFLINE_SEC,   # heartbeat lost -> node offline
    "diary_enabled": True,                  # let Flori write weekly entries
    "diary_interval_days": envf("DIARY_INTERVAL_DAYS", 7.0),
    # Flori's autonomy over THIS plant: off (advice only) | ask | auto
    "ai_control": "off",
}
NUMERIC_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, (int, float))}
BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}

# `node_fresh()` runs in hot paths (per node, every tick and every request), so the
# global config is cached for a couple of seconds instead of hitting SQLite each time.
_cfg_cache: dict[str, Any] = {"t": 0.0, "v": None}
CFG_CACHE_TTL = 3.0


def _cache_bust() -> None:
    _cfg_cache["t"] = 0.0

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
            CREATE TABLE IF NOT EXISTS node_config (
                node TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY (node, key)
            );
            """
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
            CREATE TABLE IF NOT EXISTS diary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node TEXT NOT NULL, ts REAL NOT NULL, text TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
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


def _global_cached() -> dict[str, Any]:
    """Cached global config for hot paths (node_fresh, ticks)."""
    now = time.time()
    v = _cfg_cache["v"]
    if v is None or now - _cfg_cache["t"] > CFG_CACHE_TTL:
        v = get_config()
        _cfg_cache["v"] = v
        _cfg_cache["t"] = now
    return v


def set_config(updates: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for k, v in updates.items():
        if k not in DEFAULTS:
            continue
        try:
            if k in BOOL_KEYS:
                v = _as_bool(v)
            elif k in NUMERIC_KEYS:
                v = max(0.0, float(v))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"invalid value for {k!r}")
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
        _cache_bust()
    return clean


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("1", "true", "on", "yes", "ja")


def get_config_for(node_name: str) -> dict[str, Any]:
    """Effective config for one node = global defaults overridden by this node."""
    cfg = dict(get_config())
    with _db_lock:
        rows = db().execute("SELECT key,value FROM node_config WHERE node=?", (node_name,)).fetchall()
    for r in rows:
        try:
            cfg[r["key"]] = json.loads(r["value"])
        except json.JSONDecodeError:
            pass
    return cfg


def set_config_for(node_name: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Per-node override. Keys not in DEFAULTS are ignored. A value of None
    removes the override (falls back to the global value)."""
    clean: dict[str, Any] = {}
    with _db_lock:
        c = db()
        for k, v in updates.items():
            if k not in DEFAULTS:
                continue
            if v is None:
                c.execute("DELETE FROM node_config WHERE node=? AND key=?", (node_name, k))
                clean[k] = None
                continue
            try:
                if k in BOOL_KEYS:
                    v = _as_bool(v)
                elif k in NUMERIC_KEYS:
                    v = max(0.0, float(v))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"invalid value for {k!r}")
            c.execute(
                "INSERT INTO node_config(node,key,value,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(node,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (node_name, k, json.dumps(v), time.time()),
            )
            clean[k] = v
        c.commit()
    _cache_bust()
    return clean


def audit(actor: str, action: str, detail: str = "", ip: str | None = None) -> None:
    with _db_lock:
        c = db()
        c.execute(
            "INSERT INTO audit(ts,actor,ip,action,detail) VALUES(?,?,?,?,?)",
            (time.time(), actor, ip, action, detail),
        )
        c.commit()


def meta_get(key: str, default: str | None = None) -> str | None:
    with _db_lock:
        row = db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key: str, value: str | None) -> None:
    with _db_lock:
        c = db()
        if value is None:
            c.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            c.execute(
                "INSERT INTO meta(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), time.time()),
            )
        c.commit()


def meta_map(prefix: str = "") -> dict[str, str]:
    with _db_lock:
        rows = db().execute("SELECT key,value FROM meta WHERE key LIKE ?", (prefix + "%",)).fetchall()
    return {r["key"]: r["value"] for r in rows}


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
        "offline_since": None,
        "metrics_ts": 0.0,   # when metrics were last refreshed (stale-data guard)
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
STATE_LOCK = threading.RLock()   # guards per-node field mutation vs. serialization
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
        client.subscribe(TOPIC_EVENT_WILDCARD, qos=1)
        log.info("MQTT connected, subscribed to %s + %s + %s",
                 TOPIC_TELEMETRY_WILDCARD, TOPIC_STATUS_WILDCARD, TOPIC_EVENT_WILDCARD)
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
        now = time.time()
        with STATE_LOCK:
            # If telemetry is still arriving, the node is alive no matter what a
            # *retained* status says (the broker replays the last-will offline on
            # every connect). A live, non-retained will is authoritative.
            heartbeat_ok = bool(n["last_seen"]) and (now - n["last_seen"]) < (
                float(_global_cached().get("node_offline_sec", NODE_OFFLINE_SEC)))
            if online:
                n["online"] = True
                n["offline_since"] = None
                # A retained birth is replayed on every API reconnect: only trust it
                # to refresh last_seen when the node already looks alive.
                if not msg.retain or heartbeat_ok:
                    n["last_seen"] = now
            elif not msg.retain or not heartbeat_ok:
                # Genuine last-will (or nothing better to go on): mark it down. We
                # pull last_seen back so the API shows "offline" immediately.
                n["online"] = False
                n["offline_since"] = now
                n["last_seen"] = min(n["last_seen"] or now, now)
                n["metrics_ts"] = 0.0
        broadcast({"type": "status", "node": node_name, "online": online})
        return

    if kind == "event":
        text = msg.payload.decode(errors="replace").strip()[:200]
        node(node_name)
        log.info("[%s] event: %s", node_name, text or "(empty)")
        broadcast({"type": "event", "node": node_name, "event": text})
        audit("node", f"event_{text or 'unknown'}", node_name)
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
    now = time.time()
    with STATE_LOCK:
        n["metrics"].update({k: v for k, v in payload.items()
                             if k not in ("ts", "device", "fw")})
        n["metrics_ts"] = now
        n["last_seen"] = now
        n["online"] = True
        n["offline_since"] = None
        n["last_msg_ts"] = float(payload.get("ts") or now)
        n["device"] = payload["device"]
        # A node that was running but now reports a small uptime has just restarted.
        # This is how we catch the classic pump-inrush brownout reset.
        on_s = payload.get("on_s")
        if isinstance(on_s, (int, float)):
            previous = n.get("on_s")
            if previous is not None and on_s < previous:
                n["restarts"] = n.get("restarts", 0) + 1
                n["last_restart_ts"] = now
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
        info = MQTT_CLIENT.publish(topic, json.dumps(payload), qos=1, retain=retain)
        # rc == 0 means the message was accepted for delivery. Previously we returned
        # True unconditionally, so callers reported success even for a dropped send.
        return info.rc == mqtt.MQTT_ERR_SUCCESS
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
    """True if this node's last telemetry arrived recently enough to trust.

    Freshness is a function of *time* only. A status/LWT message can update the
    flags, but it must never make a node that is actively sending telemetry look
    dead — the retained "offline" last-will is replayed on every (re)connect and
    would otherwise flap healthy nodes.
    """
    last = n.get("last_seen") or 0.0
    if not last:
        return False
    try:
        timeout = float(_global_cached().get("node_offline_sec", NODE_OFFLINE_SEC))
    except (TypeError, ValueError):
        timeout = NODE_OFFLINE_SEC
    return (time.time() - last) < timeout


def metrics_fresh(n: dict[str, Any]) -> bool:
    """True if the *readings* are recent (rules must not act on stale metrics)."""
    return node_fresh(n) and bool(n.get("metrics_ts"))


def is_fresh() -> bool:
    """True if at least one node is fresh."""
    return any(node_fresh(NODES[k]) for k in node_names())


async def _rules_for_node(node_name: str, n: dict[str, Any]) -> None:
    cfg = get_config_for(node_name)
    now = time.time()
    last_seen = n["last_seen"]
    age = now - last_seen if last_seen else 1e9
    label = n.get("device", node_name)
    fresh = node_fresh(n)   # false when LWT offline or heartbeat too old

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
    elif fresh:
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
        and fresh                        # never act on stale readings / dead node
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
    dry = fresh and moisture is not None and moisture < cfg["alert_dry_pct"]
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
    if fresh and tank is not None and tank <= cfg["alert_tank_pct"]:
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
    # The estop topic is retained and only published when it actually changes
    # (see /api/estop, /api/resume). Republishing it every 5 s made the firmware
    # reprocess it constantly, which flooded its main loop. Publish once per node.
    if n.get("_estop_sent") != n["halted"]:
        publish(t_estop(node_name), {"estop": n["halted"]}, retain=True)
        n["_estop_sent"] = n["halted"]
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

ALLOWED_FIELDS = {"moisture_pct", "temp_c", "humidity", "lux", "tank_pct", "rssi",
                  "soil_v", "ldr_v", "battery_pct"}


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


def memory_history(field: str, hours: float, node_name: str | None = None,
                   every: str = "1m") -> list[dict[str, Any]]:
    cutoff = time.time() - hours * 3600
    step = {"10s": 10, "1m": 60, "5m": 300, "15m": 900, "1h": 3600}.get(every, 60)
    src = node_history(node_name) if node_name else [p for k in node_names() for p in node_history(k)]
    # Bucket + average so the memory fallback has the same shape as Influx
    # (previously it returned raw points and ignored the `every` parameter).
    buckets: dict[int, list[float]] = {}
    for s in src:
        ts = s.get("ts", 0)
        v = s.get(field)
        if ts < cutoff or not isinstance(v, (int, float)):
            continue
        b = int(ts // step) * step
        buckets.setdefault(b, []).append(float(v))
    return [
        {"t": datetime.fromtimestamp(b, timezone.utc).isoformat(), "v": sum(vs) / len(vs)}
        for b, vs in sorted(buckets.items())
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
    with STATE_LOCK:
        age = time.time() - n["last_seen"] if n["last_seen"] else None
        return {
            "node": name,
            "device": n.get("device", name),
            # copy the live dict: the MQTT thread may be updating it right now
            "metrics": dict(n["metrics"]),
            "pump": n["pump"],
            "fault": n.get("fault"),
            "alerts": list(n["alerts"]),
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
    _reload_secrets()
    threading.Thread(target=start_mqtt, daemon=True).start()
    t1 = asyncio.create_task(_loop_task(rules_tick, 5))
    t2 = asyncio.create_task(_loop_task(state_tick, 5))
    t3 = asyncio.create_task(diary_tick())
    log.info("FloraHome API up (%d AI models configured)", 1 if AI_API_KEY else 0)
    yield
    t1.cancel()
    t2.cancel()
    t3.cancel()


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


def _smart_overrides(care: dict[str, Any]) -> dict[str, Any]:
    """Derive per-node settings from a species' care range, so picking a plant
    makes it behave individually without the user tuning numbers."""
    if not care:
        return {}
    lo = float(care.get("moisture_min_pct", 30))
    hi = float(care.get("moisture_max_pct", 65))
    interval = float(care.get("watering_interval_days", 7))
    over: dict[str, Any] = {
        # water when we drop a little below the species' lower bound
        "pump_threshold_pct": max(5.0, lo - 5.0),
        # alert if still below the midpoint for a while
        "alert_dry_pct": max(5.0, (lo + hi) / 2 - 5.0),
        # water less often, in a dose matched to a species that dislikes wet feet
        "pump_cooldown_min": max(60.0, interval * 24 * 60 / 3.0),
        "pump_max_per_day": max(1.0, round(3.0 / max(1.0, interval / 2.0), 1)),
    }
    if "light_min_lux" in care:
        over["light_on_below_lux"] = float(care["light_min_lux"])
        over["light_auto"] = True
    return over


@app.put("/api/plant/{node}")
async def api_put_plant(
    node: str,
    data: dict[str, Any],
    request: Request,
    planter_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(request, planter_session)
    plant = upsert_plant(node, data)
    applied: dict[str, Any] = {}
    # Smart defaults: when a species is chosen, set this node's watering values
    # from its care range (unless the caller explicitly disabled it).
    if plant.get("care") and data.get("smart_defaults", True):
        over = _smart_overrides(plant["care"])
        if over:
            applied = set_config_for(node, over)
    audit(user, "plant_profile_saved", f"{node}: {plant.get('name')}", client_ip(request))
    return {"ok": True, "plant": plant, "applied_config": applied}


@app.get("/api/plants/assigned/all")
async def api_plants_assigned() -> dict[str, Any]:
    return {"plants": list_plants()}


# ---- devices + flashing (dashboard) ----------------------------------------- #

FLASHER_URL = env("FLASHER_URL", "http://host.docker.internal:6053")
COMPOSE_DIR = env("COMPOSE_DIR", "/configs")
NODES_DIR = env("NODES_DIR", "/nodes")


def _available_nodes() -> list[str]:
    """Node configs that can be flashed (shared from the repo's esphome/)."""
    names: list[str] = []
    try:
        for f in os.listdir(NODES_DIR):
            if f.endswith(".yaml") and "secrets" not in f and f != "smartplanter.yaml":
                names.append(f[:-5])
    except OSError:
        pass
    return sorted(names)


async def _helper_post(path: str, body: dict[str, Any], timeout: float = 900) -> dict[str, Any]:
    return await _helper("POST", path, body, timeout=timeout)


@app.get("/api/devices")
async def api_devices() -> dict[str, Any]:
    """Attached ESPs + known devices. Prefers the host helper (real MACs)."""
    live = await _helper("GET", "/devices", timeout=90)
    attached = live.get("devices", [])
    if "error" in live:  # fall back to the container flasher's simple view
        fl = await _helper("GET", "/devices") if False else None
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                attached = (await client.get(f"{FLASHER_URL}/devices")).json().get("devices", [])
        except Exception:  # noqa: BLE001
            attached = []
    known = {d["id"]: d for d in list_devices()}
    for dev in attached:
        dev_id = dev.get("mac") or dev.get("port")
        if dev_id and dev_id not in known:
            upsert_device(dev_id, {"kind": "esp32", "port": dev.get("port"),
                                   "chip": dev.get("chip"), "mac": dev.get("mac"),
                                   "last_seen": time.time()})
    return {"attached": attached, "known": list_devices(),
            "helper": live.get("error") if live.get("error") else "ok"}


@app.get("/api/nodes/available")
async def api_nodes_available() -> dict[str, Any]:
    """Node configs available to flash."""
    nodes = _available_nodes()
    if not nodes:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                nodes = (await client.get(f"{FLASHER_URL}/nodes")).json().get("nodes", [])
        except Exception:  # noqa: BLE001
            pass
    return {"nodes": nodes}


@app.post("/api/flash")
async def api_flash(
    body: dict[str, Any],
    request: Request,
    planter_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """Flash a node over USB. The host helper does the esptool/esphome work."""
    user = require_user(request, planter_session)
    node = str(body.get("node", ""))
    port = str(body.get("port", "/dev/ttyUSB0"))
    if not node:
        raise HTTPException(status_code=400, detail="node required")
    audit(user, "flash_requested", f"{node} on {port}", client_ip(request))
    result = await _helper_post("/flash", {"node": node, "port": port})
    if result.get("helper") == "unavailable":
        raise HTTPException(status_code=503, detail="host helper unavailable (install deploy/install-host-helper.sh)")
    audit(user, "flash_done" if result.get("ok") else "flash_failed",
          f"{node} on {port}", client_ip(request))
    return result


# ---- Voice: talk to the plants (Pi speaker + mic) --------------------------- #
# The heavy lifting (speech-to-text, text-to-speech) is done by the configured
# AI provider. Audio capture/playback happens on the Pi (arecord / aplay) via
# the host helper, so the browser never needs mic permission on the kiosk.

@app.post("/api/voice/stt")
async def api_voice_stt(body: dict[str, Any], request: Request,
                        planter_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Transcribe browser-recorded audio (base64). Keeps STT server-side so the
    API key never reaches the client; recording happens on whatever device has a
    microphone (phone, laptop, tablet)."""
    require_user(request, planter_session)
    audio = body.get("audio")
    if not audio:
        raise HTTPException(status_code=400, detail="audio (base64) required")
    try:
        text = await _ai_stt(base64.b64decode(audio))
    except HTTPException as exc:
        raise HTTPException(status_code=502, detail=f"transcription failed: {exc.detail}")
    return {"ok": True, "text": text}


@app.get("/api/voice/devices")
async def api_voice_devices() -> dict[str, Any]:
    """Audio devices on the Pi (for the optional 'play on Pi' mode)."""
    return await _helper("GET", "/audio")


@app.post("/api/voice/record")
async def api_voice_record(body: dict[str, Any], request: Request,
                           planter_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Record a few seconds from the Pi's microphone (via the host helper).

    Note: the Pi has no built-in mic — this only works with a USB microphone.
    The browser mic (used by the dashboard) is the recommended path."""
    require_user(request, planter_session)
    secs = int(max(1, min(int(body.get("seconds", 4)), 15)))
    res = await _helper("POST", "/audio/record", {"seconds": secs}, timeout=secs + 20)
    if not res.get("ok") and "error" not in res:
        res["error"] = "arecord failed – the Pi has no built-in mic (plug a USB mic, or use the browser mic)"
    return res


@app.post("/api/voice/tts")
async def api_voice_tts(body: dict[str, Any], request: Request,
                        planter_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Text-to-speech. Returns a base64 wav so the *browser* can play it, or (if
    `play` is set) also plays it on the Pi via the host helper."""
    user = require_user(request, planter_session)
    text = str(body.get("text", ""))[:1500]
    fmt = str(body.get("format", "wav"))
    lang = _norm_lang(body.get("lang"))
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    wav = await _ai_tts(text, fmt, lang=lang)
    played = False
    if body.get("play") and wav:
        played = (await _helper("POST", "/audio/play", {"data": base64.b64encode(wav).decode()},
                                timeout=60)).get("ok", False)
    return {"ok": True, "played": played, "bytes": len(wav or b""),
            "audio": base64.b64encode(wav).decode() if wav else None}


@app.post("/api/voice/ask")
async def api_voice_ask(body: dict[str, Any], request: Request,
                        planter_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Full voice turn: audio (base64 wav) or text -> grounded reply -> speech.
    Returns the spoken audio as base64 so the requesting device plays it; if
    `play` is set, it is also played on the Pi speaker."""
    user = require_user(request, planter_session)
    node_name = str(body.get("node") or _primary_name())
    lang = _norm_lang(body.get("lang"))
    question = str(body.get("text", "")).strip()
    if not question and body.get("audio"):
        try:
            question = await _ai_stt(base64.b64decode(body["audio"]), lang=lang)
        except HTTPException as exc:
            raise HTTPException(status_code=502, detail=f"transcription failed: {exc.detail}")
    if not question:
        raise HTTPException(status_code=400, detail="no text and no audio")

    system = (
        _t("voice.system", lang) + "\n\n" + _plant_context(node_name, lang)
    )
    reply = await _ai_chat([{"role": "system", "content": system},
                            {"role": "user", "content": question}], max_tokens=600)
    played = False
    speak_error = None
    audio_b64 = None
    if body.get("speak", True):
        wav = await _ai_tts(reply, "wav", lang=lang)
        if not wav:
            speak_error = _t("voice.tts_failed", lang)
        else:
            audio_b64 = base64.b64encode(wav).decode()
            if body.get("play"):
                res = await _helper("POST", "/audio/play", {"data": audio_b64}, timeout=90)
                played = bool(res.get("ok"))
                if not played:
                    speak_error = res.get("error") or res.get("out") or "playback failed"
    audit("system", "voice_ask", f"{node_name}: {question[:60]}", client_ip(request))
    return {"node": node_name, "question": question, "reply": reply,
            "played": played, "speak_error": speak_error, "audio": audio_b64}


# ---- AI plant diary --------------------------------------------------------- #

DIARY_DAYS = envf("DIARY_DAYS", 7)


def _diary_points(node_name: str) -> dict[str, Any]:
    """Coarse stats about the last week from the in-memory fallback history."""
    hist = list(node_history(node_name))
    cutoff = time.time() - DIARY_DAYS * 86400
    recent = [h for h in hist if h.get("ts", 0) >= cutoff]

    def stats(key: str) -> dict[str, Any]:
        vals = [float(h[key]) for h in recent if isinstance(h.get(key), (int, float))]
        if not vals:
            return {}
        return {"min": round(min(vals), 1), "max": round(max(vals), 1),
                "avg": round(sum(vals) / len(vals), 1), "n": len(vals)}
    return {"moisture": stats("moisture_pct"), "temp": stats("temp_c"),
            "humidity": stats("humidity"), "lux": stats("lux"), "samples": len(recent)}


def add_diary(node_name: str, text: str) -> None:
    with _db_lock:
        db().execute("INSERT INTO diary(node,ts,text) VALUES(?,?,?)",
                     (node_name, time.time(), text))
        db().commit()


def diary_for(node_name: str, limit: int = 12) -> list[dict[str, Any]]:
    with _db_lock:
        rows = db().execute("SELECT * FROM diary WHERE node=? ORDER BY ts DESC LIMIT ?",
                            (node_name, max(1, min(limit, 100)))).fetchall()
    return [dict(r) for r in rows]


async def _write_diary(node_name: str, lang: str = "en") -> str | None:
    n = node(node_name)
    if not AI_API_KEY or not node_fresh(n):
        return None
    plant = get_plant(node_name) or {}
    stats = _diary_points(node_name)
    prompt = _t(
        "diary.prompt", lang,
        name=plant.get("nickname") or plant.get("name") or node_name,
        species=plant.get("species") or "unknown",
        stats=stats,
        moist=n["metrics"].get("moisture_pct"),
        temp=n["metrics"].get("temp_c"),
        lux=n["metrics"].get("lux"),
        pump=n["pump"],
        count=n["pump_count_today"],
        fault=n.get("fault"),
    )
    try:
        text = await _ai_chat([{"role": "user", "content": prompt}], max_tokens=350)
    except HTTPException as exc:
        log.warning("diary failed for %s: %s", node_name, exc.detail)
        return None
    add_diary(node_name, text)
    audit("system", "ai_diary", node_name)
    broadcast({"type": "diary", "node": node_name, "text": text})
    return text


def _newest_diary_ts(node_name: str) -> float:
    with _db_lock:
        row = db().execute("SELECT ts FROM diary WHERE node=? ORDER BY ts DESC LIMIT 1",
                           (node_name,)).fetchone()
    return float(row["ts"]) if row else 0.0


async def diary_tick() -> None:
    """Write a diary entry per fresh node when its interval is due.

    Due-ness is derived from the newest stored entry, so restarting the API no
    longer resets the weekly timer (the old version slept 168 h before the first
    write and lost that timer on every restart)."""
    await asyncio.sleep(60)   # let MQTT connect and nodes report once
    while True:
        for name in node_names():
            cfg = get_config_for(name)
            if not cfg.get("diary_enabled", True):
                continue
            try:
                days = float(cfg.get("diary_interval_days", DIARY_DAYS))
            except (TypeError, ValueError):
                days = DIARY_DAYS
            if time.time() - _newest_diary_ts(name) >= max(1.0, days) * 86400:
                await _write_diary(name)   # no-ops safely while the node is offline
        await asyncio.sleep(600)


# ---- AI (plant chat + photo identification) --------------------------------- #
# Provider-agnostic OpenAI-compatible endpoint. Set AI_BASE_URL + AI_API_KEY +
# AI_MODEL in .env. Works with a free Gemini key, OpenRouter :free models, Groq,
# ModelScope, etc. If unset, the feature is simply reported as unavailable.

# ---- i18n (EN default, DE, NL) --------------------------------------------- #
# The dashboard sends its UI language on every request; server-generated strings
# (alerts, AI prompts, voice) follow suit. English is the fallback.
LANGS = ("en", "de", "nl")
LANG_NAMES = {"en": "English", "de": "Deutsch", "nl": "Nederlands"}

STRINGS: dict[str, dict[str, str]] = {
    # voice + AI
    "voice.system": {
        "en": "You are FlorAI, FloraHome's plant assistant's warm plant companion, speaking out loud. "
              "Reply in English, short and clear (1-3 sentences, no emojis, no markdown), "
              "using ONLY the data given.",
        "de": "Du bist FlorAI, der warme Pflanzen-Begleiter von FloraHome, und sprichst laut. "
              "Antworte auf Deutsch, kurz und klar (1-3 Sätze, keine Emojis, kein Markdown), "
              "NUR mit den gegebenen Daten.",
        "nl": "Je bent FlorAI, het warme plantenmaatje van FloraHome, en je spreekt hardop. "
              "Antwoord in het Nederlands, kort en duidelijk (1-3 zinnen, geen emoji's, "
              "geen markdown), gebruik ALLEEN de gegeven gegevens.",
    },
    "voice.tts_failed": {
        "en": "no speech output from the AI provider",
        "de": "keine Sprachausgabe vom KI-Anbieter",
        "nl": "geen spraakuitvoer van de AI-provider",
    },
    "voice.stt_prompt": {
        "en": "Transcribe the spoken words. Reply with ONLY the text.",
        "de": "Transkribiere die gesprochenen Worte. Antworte NUR mit dem Text.",
        "nl": "Transcribeer de gesproken woorden. Antwoord ALLEEN met de tekst.",
    },
    "chat.system": {
        "en": "You are FlorAI, FloraHome's plant assistant's friendly plant companion, speaking as the plant "
              "itself (first person). Reply in English, warm and concrete, using ONLY the "
              "given data. If the plant is unwell, explain what the readings mean and what "
              "the human should do. Always write complete sentences. Reply in 2-4 sentences.",
        "de": "Du bist FlorAI, der freundliche Pflanzen-Begleiter von FloraHome und sprichst "
              "als die Pflanze selbst (ich-Form). Antworte auf Deutsch, warm und konkret, "
              "NUR mit den gegebenen Daten. Wenn es der Pflanze nicht gut geht, sag was die "
              "Werte bedeuten und was der Mensch tun soll. Schreibe immer vollständige Sätze. "
              "Antworte in 2-4 Sätzen.",
        "nl": "Je bent FlorAI, het vriendelijke plantenmaatje van FloraHome, en je spreekt als "
              "de plant zelf (ik-vorm). Antwoord in het Nederlands, warm en concreet, gebruik "
              "ALLEEN de gegeven gegevens. Als het niet goed gaat met de plant, leg uit wat de "
              "waarden betekenen en wat de mens moet doen. Schrijf altijd volledige zinnen. "
              "Antwoord in 2-4 zinnen.",
    },
    "identify.system": {
        "en": "You are a plant identifier. Reply ONLY with JSON: "
              '{"common":"...","scientific":"...","confidence":0-1,"care_hint":"..."}. ',
        "de": "Du bist ein Pflanzenbestimmer. Antworte NUR mit JSON: "
              '{"common":"...","scientific":"...","confidence":0-1,"care_hint":"..."}. ',
        "nl": "Je bent een plantenherkenner. Antwoord ALLEEN met JSON: "
              '{"common":"...","scientific":"...","confidence":0-1,"care_hint":"..."}. ',
    },
    "identify.prefer": {
        "en": "Prefer these species if they fit: ",
        "de": "Bevorzuge diese Arten, wenn sie passen: ",
        "nl": "Geef deze soorten voorrang als ze passen: ",
    },
    "chat.applied": {
        "en": "Done – I've updated the plant's settings.",
        "de": "Erledigt – ich habe die Einstellungen der Pflanze angepasst.",
        "nl": "Klaar – ik heb de instellingen van de plant aangepast.",
    },    "diary.prompt": {
        "en": "Write a SHORT, cute diary entry (1-3 short sentences, English, max ~60 words) "
              "as the plant '{name}' ({species}), in first person. Sprinkle in 1-2 fitting "
              "emojis. Use this week: {stats}. Now: soil {moist}%, {temp}C, light {lux}lx, "
              "pump {pump} ({count}x today), fault={fault}. Be warm, playful and concrete.",
        "de": "Schreibe einen KURZEN, niedlichen Tagebuch-Eintrag (1-3 kurze Sätze, Deutsch, "
              "max. ~60 Wörter) als die Pflanze '{name}' ({species}), in Ich-Form. Baue 1-2 "
              "passende Emojis ein. Diese Woche: {stats}. Jetzt: Bodenfeuchte {moist}%, "
              "{temp}C, Licht {lux}lx, Pumpe {pump} ({count}x heute), fault={fault}. Sei warm, "
              "verspielt und konkret.",
        "nl": "Schrijf een KORT, schattig dagboekbijdrage (1-3 korte zinnen, Nederlands, "
              "max. ~60 woorden) als de plant '{name}' ({species}), in de ik-vorm. Strooi er "
              "1-2 passende emoji's in. Deze week: {stats}. Nu: bodem {moist}%, {temp}C, "
              "licht {lux}lx, pomp {pump} ({count}x vandaag), fault={fault}. Wees warm, speels "
              "en concreet.",
    },
}


def _norm_lang(value: Any) -> str:
    """Map anything ('de-DE', 'nl', None…) onto a supported language code."""
    if not value:
        return "en"
    v = str(value).strip().lower().replace("_", "-")
    base = v.split("-")[0]
    return base if base in LANGS else "en"


def _t(key: str, lang: str = "en", **kw: Any) -> str:
    """Look up a localized string, fill placeholders, fall back to English."""
    lang = _norm_lang(lang)
    entry = STRINGS.get(key, {})
    text = entry.get(lang) or entry.get("en") or key
    if kw:
        try:
            text = text.format(**kw)
        except (KeyError, IndexError):
            pass
    return text


# ---- AI (plant chat + photo identification) --------------------------------- #
# Provider-agnostic OpenAI-compatible endpoint. Set AI_BASE_URL + AI_API_KEY +
# AI_MODEL in .env. Works with a free Gemini key, OpenRouter :free models, Groq,
# ModelScope, etc. If unset, the feature is simply reported as unavailable.

AI_BASE_URL = env("AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
AI_API_KEY = env("AI_API_KEY", "")
AI_MODEL = env("AI_MODEL", "gemini-3.8-flash")
AI_VISION_MODEL = env("AI_VISION_MODEL", AI_MODEL)
# Fallbacks tried in order when a model is busy/unavailable (503) or gone (404).
AI_FALLBACKS = [m.strip() for m in env(
    "AI_FALLBACKS", "gemini-3.8-flash,gemini-flash-latest,gemini-3.6-flash,gemini-2.5-flash,gemma-4-31b-it"
).split(",") if m.strip()]


# Native Gemini endpoint (TTS/STT audio is not on the OpenAI-compat path).
AI_NATIVE_BASE = env("AI_NATIVE_BASE", "https://generativelanguage.googleapis.com/v1beta")


async def _ai_stt(audio_bytes: bytes, lang: str = "en") -> str:
    """Speech-to-text via the native generateContent endpoint: send the wav as
    inline data and ask for a transcript."""
    if not AI_API_KEY:
        raise HTTPException(status_code=503, detail="AI not configured")
    payload = {
        "contents": [{"parts": [
            {"text": _t("voice.stt_prompt", lang)},
            {"inlineData": {"mimeType": "audio/wav",
                            "data": base64.b64encode(audio_bytes).decode()}},
        ]}],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{AI_NATIVE_BASE}/models/{env('AI_STT_MODEL', AI_MODEL)}:generateContent",
            params={"key": AI_API_KEY}, json=payload,
        )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"STT {r.status_code}: {r.text[:160]}")
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError):
        return ""


async def _ai_tts(text: str, fmt: str = "wav", lang: str = "en") -> bytes:
    """Text-to-speech via the native generateContent endpoint (returns wav).
    Falls back to the OpenAI-compat path for other providers."""
    if not AI_API_KEY:
        return b""
    spoken = text if _norm_lang(lang) == "en" else f"[{LANG_NAMES[_norm_lang(lang)]}] {text}"
    native_models = [env("AI_TTS_MODEL", "gemini-3.8-flash-tts"),
                     "gemini-2.5-flash-preview-tts"]
    voice = env("AI_TTS_VOICE", "Kore")
    async with httpx.AsyncClient(timeout=60) as client:
        for m in native_models:
            try:
                r = await client.post(
                    f"{AI_NATIVE_BASE}/models/{m}:generateContent",
                    params={"key": AI_API_KEY},
                    json={
                        "contents": [{"parts": [{"text": spoken}]}],
                        "generationConfig": {
                            "responseModalities": ["AUDIO"],
                            "speechConfig": {"voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": voice}}},
                        },
                    },
                )
                if r.status_code < 400:
                    for p in r.json()["candidates"][0]["content"]["parts"]:
                        inline = p.get("inlineData")
                        if inline and inline.get("data"):
                            return base64.b64decode(inline["data"])
            except Exception:  # noqa: BLE001
                continue
        # OpenAI-compatible providers that do support /audio/speech
        for m in native_models:
            try:
                r = await client.post(
                    f"{AI_BASE_URL.rstrip('/')}/audio/speech",
                    headers={"Authorization": f"Bearer {AI_API_KEY}"},
                    json={"model": m, "input": text, "voice": voice, "response_format": fmt,
                          "language": _norm_lang(lang)},
                )
                if r.status_code < 400 and r.content:
                    return r.content
            except Exception:  # noqa: BLE001
                continue
    return b""


async def _ai_chat(messages: list[dict[str, Any]], model: str | None = None,
                   max_tokens: int = 400) -> str:
    if not AI_API_KEY:
        raise HTTPException(status_code=503, detail="AI not configured (set AI_API_KEY)")
    tried: list[str] = []
    candidates = [model or AI_MODEL] + [m for m in AI_FALLBACKS if m != (model or AI_MODEL)]
    last = ""
    async with httpx.AsyncClient(timeout=60) as client:
        for m in candidates:
            tried.append(m)
            payload = {"model": m, "messages": messages,
                       "max_tokens": max_tokens, "temperature": 0.7}
            try:
                r = await client.post(
                    f"{AI_BASE_URL.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {AI_API_KEY}",
                             "Content-Type": "application/json"},
                    json=payload,
                )
            except Exception as exc:  # noqa: BLE001
                last = str(exc); continue
            if r.status_code < 400:
                data = r.json()
                choice = (data.get("choices", [{}])[0] or {})
                text = ((choice.get("message", {}) or {}).get("content") or "").strip()
                # Some providers return reasoning-only or truncate. If the reply
                # was cut off (finish_reason == length), retry once with more room.
                if choice.get("finish_reason") == "length" and max_tokens < 1600:
                    return await _ai_chat(messages, model=m, max_tokens=1600)
                return text
            last = f"{r.status_code}: {r.text[:160]}"
            if r.status_code not in (429, 503, 404, 500):
                break
    raise HTTPException(status_code=502, detail=f"AI unavailable (tried {', '.join(tried)}): {last}")


# ---- Flori's plant-control tools ------------------------------------------- #
# Flori can read state and, subject to a per-plant autonomy level, change the
# plant's settings/automations and trigger a *bounded* watering dose or light.
# Safety rails are absolute: they are enforced here regardless of what the model
# asks for, and the ESP32 has its own firmware ceiling as the final backstop.

AI_TOOLS = [
    {"type": "function", "function": {
        "name": "get_plant_state",
        "description": "Read the live telemetry, plant profile and current watering/light settings for a plant.",
        "parameters": {"type": "object", "properties": {
            "node": {"type": "string", "description": "Node id, e.g. plant-a"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "set_watering",
        "description": "Change the automatic watering settings for a plant.",
        "parameters": {"type": "object", "properties": {
            "node": {"type": "string"},
            "auto": {"type": "boolean", "description": "Enable/disable automatic watering"},
            "threshold_pct": {"type": "number", "description": "Water when soil moisture is below this %"},
            "seconds": {"type": "number", "description": "Pump run time per dose (1-45 s)"},
            "cooldown_min": {"type": "number", "description": "Minimum minutes between doses"},
            "max_per_day": {"type": "number", "description": "Hard cap of doses per day"},
            "dry_alert_pct": {"type": "number"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "set_light",
        "description": "Control the grow light for a plant, automatically or manually.",
        "parameters": {"type": "object", "properties": {
            "node": {"type": "string"},
            "auto": {"type": "boolean", "description": "Enable automatic light control by lux"},
            "on_below_lux": {"type": "number", "description": "Turn the light on when light drops below this"},
            "state": {"type": "string", "enum": ["on", "off"], "description": "Manually switch the light"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "apply_species_care",
        "description": "Configure a plant's watering and light from its species' care range (uses the plant profile, or a species id).",
        "parameters": {"type": "object", "properties": {
            "node": {"type": "string"},
            "species_id": {"type": "string", "description": "Optional catalog id, e.g. monstera"}},
            "required": ["node"]}}},
    {"type": "function", "function": {
        "name": "water_now",
        "description": "Start ONE bounded watering dose now (respects the per-dose and daily safety limits).",
        "parameters": {"type": "object", "properties": {
            "node": {"type": "string"},
            "seconds": {"type": "number", "description": "Dose length, 1-30 s"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "write_diary",
        "description": "Write a diary entry for a plant.",
        "parameters": {"type": "object", "properties": {
            "node": {"type": "string"},
            "text": {"type": "string"}},
            "required": ["node", "text"]}}},
]


def _ai_tool_ctx(node_name: str, lang: str) -> str:
    return _plant_context(node_name, lang) + (
        "\n\nYou have tools to adjust THIS plant. Only act when the user clearly asks.\n"
        "Preferred order for 'set up this plant': call apply_species_care (uses the plant's "
        "species care range), then explain in a sentence what you changed. Keep doses short.\n"
        "Never water if the soil is already moist or the plant is offline."
    )


async def _run_ai_tool(name: str, args: dict[str, Any], node_name: str, user: str,
                       ip: str | None, level: str) -> dict[str, Any]:
    """Execute one Flori tool with hard safety rails. `level` is off/ask/auto."""
    n = node(node_name)

    def deny(reason: str) -> dict[str, Any]:
        return {"ok": False, "error": reason}

    if level == "off" and name not in ("get_plant_state",):
        return deny("AI control is off for this plant. Suggest the change to the user instead.")

    if name == "get_plant_state":
        cfg = get_config_for(node_name)
        plant = get_plant(node_name) or {}
        return {"ok": True, "node": node_name, "online": node_fresh(n),
                "metrics": dict(n["metrics"]), "pump": n["pump"], "fault": n.get("fault"),
                "species": plant.get("species"), "care": plant.get("care"),
                "settings": {k: cfg.get(k) for k in (
                    "pump_auto", "pump_threshold_pct", "pump_seconds", "pump_cooldown_min",
                    "pump_max_per_day", "alert_dry_pct", "light_auto", "light_on_below_lux")}}

    if name == "set_watering":
        upd: dict[str, Any] = {}
        if "auto" in args:           upd["pump_auto"] = bool(args["auto"])
        if "threshold_pct" in args:  upd["pump_threshold_pct"] = float(args["threshold_pct"])
        if "seconds" in args:        upd["pump_seconds"] = max(1.0, min(float(args["seconds"]), 45.0))
        if "cooldown_min" in args:   upd["pump_cooldown_min"] = max(0.0, float(args["cooldown_min"]))
        if "max_per_day" in args:    upd["pump_max_per_day"] = max(0.0, min(float(args["max_per_day"]), 24.0))
        if "dry_alert_pct" in args:  upd["alert_dry_pct"] = float(args["alert_dry_pct"])
        applied = set_config_for(node_name, upd)
        audit(user, "ai_set_watering", f"{node_name}: {json.dumps(applied, ensure_ascii=False)}", ip)
        return {"ok": True, "applied": applied}

    if name == "set_light":
        upd = {}
        if "auto" in args:       upd["light_auto"] = bool(args["auto"])
        if "on_below_lux" in args: upd["light_on_below_lux"] = max(0.0, float(args["on_below_lux"]))
        applied = set_config_for(node_name, upd)
        if args.get("state") in ("on", "off"):
            # a manual light command is a direct actuation -> bounded + audited
            publish(t_cmd(node_name), {"action": "light", "state": args["state"], "reason": "ai"})
        audit(user, "ai_set_light", f"{node_name}: {json.dumps(applied, ensure_ascii=False)} state={args.get('state')}", ip)
        return {"ok": True, "applied": applied, "state": args.get("state")}

    if name == "apply_species_care":
        plant = get_plant(node_name) or {}
        care = plant.get("care") or {}
        sid = args.get("species_id") or plant.get("species_id")
        if sid:
            sp = load_plant_db().get(sid)
            if sp:
                care = sp.get("care") or care
        if not care:
            return deny("No species care range known for this plant. Ask the user to set a species.")
        over = _smart_overrides(care)
        applied = set_config_for(node_name, over)
        audit(user, "ai_apply_care", f"{node_name}: {json.dumps(applied, ensure_ascii=False)}", ip)
        return {"ok": True, "applied": applied, "care": care}

    if name == "water_now":
        cfg = get_config_for(node_name)
        if not node_fresh(n):
            return deny("The node is offline; refusing to water.")
        if n["halted"]:
            return deny("EMERGENCY STOP is active; watering is blocked.")
        moisture = m(n, "moisture_pct")
        if moisture is not None and moisture >= cfg["pump_threshold_pct"] + 15:
            return deny(f"Soil is already moist ({moisture:.0f}%); refusing to over-water.")
        secs = int(max(1, min(float(args.get("seconds", 10)), 30)))
        secs = min(secs, int(cfg["pump_seconds"])) if cfg.get("pump_seconds") else secs
        secs = min(secs, 45)  # firmware ceiling
        if not publish(t_cmd(node_name), {"action": "pump", "seconds": secs, "reason": "ai"}):
            return deny("MQTT unavailable; command not sent.")
        with STATE_LOCK:
            n["pump"] = "watering"; n["last_pump_ts"] = time.time()
            n["pump_count_today"] = n.get("pump_count_today", 0) + 1
        audit(user, "ai_water_now", f"{node_name}: {secs}s", ip)
        return {"ok": True, "seconds": secs}

    if name == "write_diary":
        text = str(args.get("text", ""))[:2000]
        if not text:
            return deny("Empty diary text.")
        add_diary(node_name, text)
        audit(user, "ai_write_diary", node_name, ip)
        return {"ok": True}

    return deny(f"Unknown tool {name}")


async def _ai_chat_with_tools(messages: list[dict[str, Any]], node_name: str, user: str,
                              ip: str | None, level: str, max_rounds: int = 4) -> dict[str, Any]:
    """OpenAI-style tool-calling loop. Runs the model, executes any tool calls with
    the safety rails, and feeds the results back until it produces a final answer."""
    if not AI_API_KEY:
        raise HTTPException(status_code=503, detail="AI not configured (set AI_API_KEY)")
    actions: list[dict[str, Any]] = []
    msgs = list(messages)
    async with httpx.AsyncClient(timeout=60) as client:
        for _ in range(max_rounds):
            payload = {"model": AI_MODEL, "messages": msgs, "max_tokens": 700,
                       "temperature": 0.5, "tools": AI_TOOLS, "tool_choice": "auto"}
            try:
                r = await client.post(
                    f"{AI_BASE_URL.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"},
                    json=payload)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"AI error: {exc}")
            if r.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"AI {r.status_code}: {r.text[:160]}")
            choice = (r.json().get("choices", [{}])[0] or {})
            msg = choice.get("message", {}) or {}
            calls = msg.get("tool_calls") or []
            if not calls:
                return {"reply": (msg.get("content") or "").strip(), "actions": actions}
            msgs.append(msg)
            for call in calls:
                fn = (call.get("function") or {})
                fname = fn.get("name", "")
                try:
                    fargs = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    fargs = {}
                tgt = str(fargs.get("node") or node_name)
                result = await _run_ai_tool(fname, fargs, tgt, user, ip, level)
                actions.append({"tool": fname, "node": tgt,
                                "ok": bool(result.get("ok")), "result": result})
                msgs.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": json.dumps(result, ensure_ascii=False)})
    return {"reply": "", "actions": actions}


def _plant_context(node_name: str, lang: str = "en") -> str:
    n = node(node_name)
    plant = get_plant(node_name) or {}
    care = plant.get("care") or {}
    m = n["metrics"]
    return (
        f"Plant node '{node_name}', nickname {plant.get('nickname') or '—'}, "
        f"species {plant.get('species') or plant.get('name') or 'unknown'}.\n"
        f"Current readings: moisture={m.get('moisture_pct')}%, temperature={m.get('temp_c')}C, "
        f"humidity={m.get('humidity')}%, light={m.get('lux')}lx, tank={m.get('tank_pct')}%, "
        f"pump={n['pump']}, fault={n.get('fault')}, online={node_fresh(n)}.\n"
        f"Species care ranges: {care}.\n"
        f"Answer in {LANG_NAMES[_norm_lang(lang)]}."
    )


@app.get("/api/diary/{node}")
async def api_diary(node: str, limit: int = 12) -> dict[str, Any]:
    return {"node": node, "entries": diary_for(node, limit)}


@app.post("/api/diary/{node}")
async def api_diary_write(node: str, request: Request,
                          body: dict[str, Any] | None = None,
                          planter_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    require_user(request, planter_session)
    lang = _norm_lang((body or {}).get("lang"))
    text = await _write_diary(node, lang)
    if text is None:
        raise HTTPException(status_code=503, detail="AI not configured or node offline")
    return {"ok": True, "node": node, "text": text}


@app.get("/api/ai/status")
async def api_ai_status() -> dict[str, Any]:
    return {"configured": bool(AI_API_KEY), "model": AI_MODEL, "vision_model": AI_VISION_MODEL,
            "base_url": AI_BASE_URL}


@app.post("/api/ai/chat")
async def api_ai_chat(body: dict[str, Any], request: Request,
                      planter_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Chat about a plant. Grounded in the live telemetry + species care data so
    the answer is about *this* plant, not generic."""
    require_user(request, planter_session)
    node_name = str(body.get("node") or _primary_name())
    lang = _norm_lang(body.get("lang"))
    question = str(body.get("message", ""))[:1000]
    system = _t("chat.system", lang) + "\n\n" + _ai_tool_ctx(node_name, lang)
    if body.get("tools", True):
        level = str(get_config_for(node_name).get("ai_control", "off"))
        result = await _ai_chat_with_tools(
            [{"role": "system", "content": system},
             {"role": "user", "content": question}],
            node_name, user, client_ip(request), level)
        reply = result.get("reply") or _t("chat.applied", lang)
        audit("system", "ai_chat", f"{node_name}: {question[:80]}", client_ip(request))
        return {"node": node_name, "reply": reply, "actions": result.get("actions", []),
                "ai_control": level}
    reply = await _ai_chat([{"role": "system", "content": system},
                            {"role": "user", "content": question}], max_tokens=900)
    audit("system", "ai_chat", f"{node_name}: {question[:80]}", client_ip(request))
    return {"node": node_name, "reply": reply, "actions": []}


@app.post("/api/ai/identify")
async def api_ai_identify(body: dict[str, Any], request: Request,
                          planter_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Photo -> species guess + profile draft. `image` is a base64 data URL."""
    require_user(request, planter_session)
    lang = _norm_lang(body.get("lang"))
    image = str(body.get("image", ""))
    if not image.startswith("data:image"):
        raise HTTPException(status_code=400, detail="image must be a base64 data URL")
    species_list = ", ".join(p["common_de"] for p in load_plant_db().values())
    system = _t("identify.system", lang) + _t("identify.prefer", lang) + species_list + "."
    content = [
        {"type": "text", "text": "Identify the plant in the photo."},
        {"type": "image_url", "image_url": {"url": image}},
    ]
    reply = await _ai_chat([{"role": "system", "content": system},
                            {"role": "user", "content": content}],
                           model=AI_VISION_MODEL, max_tokens=300)
    guess: dict[str, Any] = {}
    try:
        start = reply.find("{"); end = reply.rfind("}")
        guess = json.loads(reply[start:end + 1])
    except Exception:  # noqa: BLE001
        guess = {"common": reply[:120]}
    # match the guess to our catalog (best effort)
    match = None
    common = str(guess.get("common", "")).lower()
    for p in load_plant_db().values():
        if common and (common in p["common"].lower() or common in (p.get("common_de") or "").lower()
                       or p["common"].lower() in common):
            match = p["id"]; break
    audit("system", "ai_identify", f"{guess.get('common','?')} -> {match}", client_ip(request))
    return {"guess": guess, "species_id": match}


# ---- hotspot / Wi-Fi settings (applied via the host helper) ----------------- #

HOST_HELPER_URL = env("HOST_HELPER_URL", "http://host.docker.internal:6054")
HOST_HELPER_TOKEN = env("HOST_HELPER_TOKEN", "")


# ---- runtime secret management --------------------------------------------- #
# Keys can be viewed (masked) and rotated from the Backend tab. Overrides are
# stored in the `meta` table as "secret:<NAME>" and applied on top of the .env
# values without a container restart.

SECRET_SPECS: list[dict[str, Any]] = [
    {"name": "AI_API_KEY", "label": "KI-Schlüssel (AI_API_KEY)", "secret": True,
     "hint": "Google AI Studio / OpenRouter / Groq – schaltet FlorAI frei"},
    {"name": "AI_BASE_URL", "label": "KI-Endpunkt (AI_BASE_URL)", "secret": False,
     "hint": "OpenAI-kompatibel, z. B. …/v1beta/openai"},
    {"name": "AI_MODEL", "label": "KI-Modell (AI_MODEL)", "secret": False, "hint": "z. B. gemini-3.8-flash"},
    {"name": "AI_VISION_MODEL", "label": "Vision-Modell (AI_VISION_MODEL)", "secret": False,
     "hint": "für „Neue Pflanze“ aus Foto"},
    {"name": "INFLUX_TOKEN", "label": "InfluxDB-Token", "secret": True, "hint": "speist die Verlaufs-Diagramme"},
    {"name": "TELEGRAM_BOT_TOKEN", "label": "Telegram-Bot-Token", "secret": True, "hint": "für Meldungen"},
    {"name": "TELEGRAM_CHAT_ID", "label": "Telegram-Chat-ID", "secret": False, "hint": ""},
    {"name": "HOST_HELPER_TOKEN", "label": "Host-Helfer-Token", "secret": True, "hint": "Hotspot / Flashen / Audio"},
]
SECRET_NAMES = {s["name"] for s in SECRET_SPECS}

# Fallbacks captured at import (the .env values), used when an override is cleared.
_ENV_SECRETS: dict[str, str] = {
    "AI_API_KEY": AI_API_KEY,
    "AI_BASE_URL": AI_BASE_URL,
    "AI_MODEL": AI_MODEL,
    "AI_VISION_MODEL": env("AI_VISION_MODEL", ""),   # empty -> follow AI_MODEL
    "INFLUX_TOKEN": INFLUX_TOKEN,
    "TELEGRAM_BOT_TOKEN": TG_TOKEN,
    "TELEGRAM_CHAT_ID": TG_CHAT,
    "HOST_HELPER_TOKEN": HOST_HELPER_TOKEN,
}


def _reload_secrets() -> None:
    """Apply runtime overrides stored in `meta` (secret:<NAME>) on top of env."""
    global AI_API_KEY, AI_BASE_URL, AI_MODEL, AI_VISION_MODEL
    global INFLUX_TOKEN, TG_TOKEN, TG_CHAT, HOST_HELPER_TOKEN
    ov = meta_map("secret:")

    def pick(name: str) -> str | None:
        v = ov.get("secret:" + name)
        return v if v is not None else _ENV_SECRETS.get(name)

    AI_API_KEY = pick("AI_API_KEY") or ""
    AI_BASE_URL = pick("AI_BASE_URL") or _ENV_SECRETS["AI_BASE_URL"]
    AI_MODEL = pick("AI_MODEL") or _ENV_SECRETS["AI_MODEL"]
    AI_VISION_MODEL = pick("AI_VISION_MODEL") or AI_MODEL
    INFLUX_TOKEN = pick("INFLUX_TOKEN") or ""
    TG_TOKEN = pick("TELEGRAM_BOT_TOKEN") or ""
    TG_CHAT = pick("TELEGRAM_CHAT_ID") or ""
    HOST_HELPER_TOKEN = pick("HOST_HELPER_TOKEN") or ""
    log.info("secrets reloaded (AI=%s influx=%s telegram=%s)",
             "set" if AI_API_KEY else "unset", "set" if INFLUX_TOKEN else "unset",
             "set" if (TG_TOKEN and TG_CHAT) else "unset")


def _mask(spec: dict[str, Any], value: str | None) -> str:
    if not value:
        return ""
    if spec["secret"]:
        return "••••" + (value[-4:] if len(value) > 4 else "")
    return value


async def _helper(method: str, path: str, body: dict[str, Any] | None = None,
                  timeout: float = 60) -> dict[str, Any]:
    headers = {"X-Host-Token": HOST_HELPER_TOKEN}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.request(method, f"{HOST_HELPER_URL}{path}", json=body, headers=headers)
            return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "helper": "unavailable"}


@app.get("/api/hotspot")
async def api_hotspot(request: Request, planter_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    require_user(request, planter_session)
    info = await _helper("GET", "/hotspot")
    return {"owner": "host", **info}


@app.put("/api/hotspot")
async def api_hotspot_put(
    data: dict[str, Any],
    request: Request,
    planter_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """Change the FloraHome access point. Applied on the host via the helper
    (nmcli). Nodes must be re-flashed with the new SSID/PSK afterwards."""
    user = require_user(request, planter_session)
    ssid = data.get("ssid")
    psk = data.get("psk")
    if not ssid and not psk:
        raise HTTPException(status_code=400, detail="ssid and/or psk required")
    result = await _helper("POST", "/hotspot", {"ssid": ssid, "psk": psk}, timeout=120)
    ok = bool(result.get("ok"))
    audit(user, "hotspot_changed" if ok else "hotspot_change_failed",
          json.dumps({"ssid": ssid, "psk": "***" if psk else None}, ensure_ascii=False),
          client_ip(request))
    return {"ok": ok, "detail": "Hotspot aktualisiert – Knoten mit neuen Zugangsdaten neu flashen."
            if ok else result, "requested": {"ssid": ssid}}


@app.get("/api/config")
async def api_get_config(node: str | None = None) -> dict[str, Any]:
    """Global config, or the effective per-node config when `?node=` is given."""
    if node:
        return {"node": node, "config": get_config_for(node), "global": get_config()}
    return get_config()


@app.put("/api/config/node/{node}")
async def api_put_config_node(
    node: str,
    updates: dict[str, Any],
    request: Request,
    planter_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """Set per-node overrides (e.g. this plant's watering threshold, cooldown,
    pump seconds). A null value clears the override."""
    user = require_user(request, planter_session)
    if len(updates) > 30:
        raise HTTPException(status_code=400, detail="too many keys")
    before = get_config_for(node)
    applied = set_config_for(node, updates)
    changes = {k: {"from": before.get(k), "to": v} for k, v in applied.items() if before.get(k) != v}
    if changes:
        audit(user, "config_changed_node", f"{node}: " + json.dumps(changes, ensure_ascii=False),
              client_ip(request))
    return {"ok": True, "node": node, "applied": changes, "config": get_config_for(node)}


@app.get("/api/config/node/{node}")
async def api_get_config_node(node: str) -> dict[str, Any]:
    return {"node": node, "config": get_config_for(node), "global": get_config()}


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


# ---- runtime secrets (API keys etc.) + system info ------------------------- #

def _keys_overview() -> dict[str, Any]:
    ov = meta_map("secret:")
    out = []
    for spec in SECRET_SPECS:
        name = spec["name"]
        db_val = ov.get("secret:" + name)
        env_val = _ENV_SECRETS.get(name) or ""
        value = db_val if db_val is not None else env_val
        source = "ui" if db_val is not None else ("env" if env_val else "unset")
        out.append({**spec, "configured": bool(value), "source": source,
                    "preview": _mask(spec, value)})
    return {"keys": out}


@app.get("/api/settings/keys")
async def api_get_keys(request: Request,
                       planter_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    require_user(request, planter_session)
    return _keys_overview()


@app.put("/api/settings/keys")
async def api_put_keys(body: dict[str, Any], request: Request,
                       planter_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Set/rotate runtime secrets. A null or empty value clears the override and
    falls back to the .env value. Changes apply immediately (no restart)."""
    user = require_user(request, planter_session)
    changed: list[str] = []
    for name, value in body.items():
        if name not in SECRET_NAMES:
            continue
        if value is None or str(value).strip() == "":
            meta_set("secret:" + name, None)
        else:
            meta_set("secret:" + name, str(value).strip()[:400])
        changed.append(name)
    if changed:
        _reload_secrets()
        audit(user, "secrets_changed", "changed: " + ", ".join(sorted(changed)), client_ip(request))
    return {"ok": True, "changed": sorted(changed), **_keys_overview()}


@app.get("/api/system")
async def api_system() -> dict[str, Any]:
    """Operational overview for the Backend tab (no secrets)."""
    cfg = _global_cached()
    return {
        "api_version": app.version,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "uptime_s": round(time.time() - START_TS, 1),
        "started_at": START_TS,
        "mqtt_connected": MQTT_CONNECTED,
        "topics": {"root": TOPIC_ROOT, "telemetry": TOPIC_TELEMETRY_WILDCARD},
        "node_offline_sec": cfg.get("node_offline_sec", NODE_OFFLINE_SEC),
        "ai_configured": bool(AI_API_KEY),
        "ai_model": AI_MODEL,
        "influx_configured": bool(INFLUX_TOKEN),
        "telegram_configured": bool(TG_TOKEN and TG_CHAT),
        "host_helper": HOST_HELPER_URL,
        "db_path": DB_PATH,
        "counts": {
            "nodes": len(node_names()),
            "events": len(recent("events", 1000)),
            "audit": len(recent("audit", 1000)),
            "devices": len(list_devices()),
        },
    }


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
    pump_secs: int | None = None
    if action == "pump":
        pump_secs = int(max(1, min(float(body.get("seconds", get_config()["pump_seconds"])), 120)))
        payload["seconds"] = pump_secs
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
    # Only now that the broker accepted the command do we update local bookkeeping,
    # so a failed send can no longer consume the daily pump budget.
    if action == "pump":
        with STATE_LOCK:
            n["last_pump_ts"] = time.time()
            n["pump"] = "watering"
            n["pump_count_today"] += 1
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
                "points": memory_history(field, hours, node_name, every)}


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
