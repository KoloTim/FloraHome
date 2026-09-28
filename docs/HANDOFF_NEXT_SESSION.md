# Handoff — next session

_Very thorough, because a fresh agent/session should be able to pick this up cold._

Repo: **https://github.com/KoloTim/FloraHome** (branch `main`). Everything below
is already pushed. Tag `pre-multinode` marks the state before the multi-node
rework.

---

## 1. The deployment in one screen

| Thing | Value |
|---|---|
| Demo host | Raspberry Pi 4B, hostname `Tim`, **192.168.91.68**, user `tim` / `timtimtim` |
| SSH to Pi | works with paramiko (password); a helper script exists (see §7) |
| Stack dir | `/home/tim/smartplanter` (Docker Compose project `smartplanter`) |
| Containers | mosquitto, influxdb, telegraf, api, web, grafana, **esphome** (:6052), **flasher** (:6053) |
| Dashboard | `http://192.168.91.68:8098` (kiosk runs it full-screen on the panel) |
| API | `http://192.168.91.68:8097` (`/docs`, `/api/state`, `/health`) |
| Grafana | `:3030` (anonymous Viewer) |
| ESPHome UI | `:6052` (build + OTA) |
| **Host helper** | systemd `florahome-host-helper` on **:6054** (root; hotspot + USB flash + audio) |
| Nodes | `plant-a` = `10.42.0.10` (`5c:01:3b:be:98:f4`), `plant-b` = `10.42.0.11` (`e0:8c:fe:e5:82:f4`) |

### Network

- **eth0** → your LAN `192.168.91.0/24`, gateway `192.168.91.1` (internet).
- **wlan0** → **access point** `mode: ap`, SSID **`FloraHome`** (2.4 GHz, ch 6,
  WPA2, PSK `floraplanter`), Pi at `10.42.0.1/24` (`ipv4.method: shared` = DHCP+NAT).
- The nodes join the Pi's AP → same L2 as the broker → **OTA works**.
- **OTA is manual/on-demand**, not automatic. USB is needed only for the first
  flash (or after a Wi-Fi change). Once a node is on `FloraHome`, OTA works with
  the USB cable unplugged.

---

## 2. How to reach the Pi from a fresh session

There is no `rsync` on this Windows PC. Use **paramiko** (already installed) with
the helper scripts in the temp dir, or write your own. The pattern:

```python
import paramiko
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.91.68", username="tim", password="timtimtim",
          allow_agent=False, look_for_keys=False)   # <-- essential: skip agent keys
```
- The SSH server intermittently answers `"Not allowed at this time"`; **retry**
  (connect loop, ~4 s backoff). Helper scripts already do this.
- Run scripts via base64 to avoid quoting hell:
  `echo <b64> | base64 -d | bash`. Scripts written by PowerShell get **CRLF** —
  write them with LF or the shell errors on `$'\r'`.
- Upload files with SFTP (`sftp.put`), download with `sftp.get`.

---

## 3. Firmware / nodes

- Node configs live **directly in `esphome/`** (NOT a subdir — `!secret` resolves
  next to the file): `smartplanter.yaml` (template), `plant-a.yaml`, `plant-b.yaml`,
  `plant-c.yaml`, `battery-template.yaml`.
- Per-node MQTT: `planter/<node>/{telemetry,status,event,cmd,estop,state/<f>}`.
- Tooling: `deploy/add-node.sh` — `new <dev> "<name>" [battery]`, `list`,
  `discover`, `compile`, `flash <dev> <port>`, `ota <dev> <ip>`.
- Build on the Pi (toolchain cache present, ~4 GB in `~/.platformio`). A full
  compile is ~7 min.
- ESP32 flash gotcha: use **`--no-stub`** (Debian esptool 4.7 is missing the stub).
- `esphome run` on this Pi must include `--device` (it defaults to OTA otherwise).

### Node status right now

| Node | Sensors | fault |
|---|---|---|
| plant-a ("Moni", Monstera) | DHT11 + LDR + HW-390 wired | `ok` (needs soil calibration) |
| plant-b ("Sanse", Sans.) | **none wired** | `dht11,ldr` (expected) |

Also present on USB: `/dev/ttyUSB0` = plant-a, `/dev/ttyUSB1` = plant-b.

---

## 4. Settings are PER NODE (this matters)

- Table `node_config(node, key, value)`; `get_config_for(node)` = global config
  overridden by node overrides. `PUT /api/config/node/<node>` sets overrides
  (`null` clears one).
- Global defaults live in `DEFAULTS` in `api/app.py` (env-seeded).
- **Smart defaults**: assigning a species to a node auto-sets its watering values
  from the species' care range (`_smart_overrides`), unless `smart_defaults:false`.
- The dashboard uses `selectedNode()` (the plant selector) everywhere now — this
  was the bug where editing one plant edited another.

---

## 5. AI / voice

- Provider: Google AI Studio key in `.env` (`AI_API_KEY`). Model
  `gemini-3.8-flash` with `AI_FALLBACKS`. Endpoint: OpenAI-compatible
  `.../v1beta/openai` for **chat**; **native** `.../v1beta/models/<m>:generateContent`
  for **TTS/STT** (the OpenAI `/audio/speech` path 404s on Gemini).
- **Chat**: `/api/ai/chat` — grounded in live telemetry + species care; retries
  with more tokens if `finish_reason=length`.
- **Photo → plant**: `/api/ai/identify` (vision), used by "📷 Neue Pflanze".
- **Diary**: `_write_diary` + `diary_tick` (weekly, `DIARY_INTERVAL_H`),
  `/api/diary/<node>` GET/POST. Stored in the `diary` table.
- **Voice**: `/api/voice/{devices,record,ask,tts}`. Recording and playback go
  through the **host helper** (`arecord`/`aplay`). Verified working
  (`played:true`). **A USB microphone is still required** for input — the Pi has
  no built-in mic. Speaker = 3.5 mm jack (`card 2`) or HDMI.

---

## 6. Host helper (the thing that was broken)

`deploy/host-helper.sh`, systemd unit `florahome-host-helper`:

- Binds **`0.0.0.0:6054`** so containers can reach `host.docker.internal:6054`.
  (It used to bind `127.0.0.1` — that was the "Host-Helfer nicht erreichbar".)
- Auth: header `X-Host-Token` = `HOST_HELPER_TOKEN` (in `.env`).
- iptables guard in the unit restricts 6054 to `172.16.0.0/12` (docker) only.
- Endpoints: `/health`, `/hotspot` (GET/POST → nmcli), `/devices` (esptool MACs),
  `/flash`, `/audio` (list), `/audio/record`, `/audio/play`.
- **Gotcha**: the systemd unit sets `HOST_HELPER_BIND`; after editing the unit you
  must `systemctl daemon-reload` **and** restart, or the old bind sticks.

---

## 7. Dashboard

Single file `web/index.html` (no build step; nginx serves it directly).
Tabbed pages: **Übersicht · Steuern · KI · Backend**. Features:

- Plant moodboard cards; per-node selector drives every control.
- Backend page: technical cards + "Details" dialog (GPIO pin map, firmware, raw
  `soil_v`/`ldr_v`, RSSI, uptime/restarts, fault, MQTT topics, test command).
- Calibration, watering settings (per node), diary, history chart.
- Devices & flashing panel; voice buttons; A−/A+ scale; easter eggs.
- Touch: `deploy/touch_bridge.py` (uinput) turns the absolute-mouse panel into a
  real touchscreen; `deploy/kiosk.sh` runs Chromium kiosk (autostart).

---

## 8. Known issues / next steps

1. **Calibrate plant-a** (reads 100 %; moisture meaningless until then) — this
   also stops spurious auto-watering.
2. **Wire plant-b's sensors** (DHT11→GPIO27, LDR→GPIO35, soil→GPIO34 power GPIO25).
3. **USB mic** for voice input.
4. **OTA is manual** — decide if you want a scheduled auto-OTA (not built).
5. Chat/voice require login (writes are protected); kiosk has no saved session.
6. Battery node (`battery-template.yaml`) not yet built/flashed on real hardware.
7. Touch panel is physically single-touch (no pinch) even with the bridge.

---

## 9. Where things live

| Path | What |
|---|---|
| `api/app.py` | the whole API (multi-node state, rules, AI, voice, diary, flash) |
| `api/plants.json` | 16-species offline plant database |
| `web/index.html` | dashboard (tabs, moodboard, backend) |
| `esphome/*.yaml` | firmware per node + battery template |
| `deploy/add-node.sh` | add/compile/flash/OTA a node |
| `deploy/host-helper.sh` | privileged shim (hotspot/flash/audio) |
| `deploy/touch_bridge.py`, `deploy/kiosk.sh` | Pi display |
| `docs/ARCHITECTURE.md`, `MQTT.md`, `HOMEASSISTANT.md`, `ALERTS.md`, `AI.md`, `VOICE.md`, `BATTERY.md`, `DEVICES.md`, `TUTORIALS.md`, `STATUS.md` | docs |
| `docs/diagrams/` | rendered SVGs |

Read `docs/ARCHITECTURE.md` first, then `docs/STATUS.md`, then this file.
