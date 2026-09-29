# Handoff — next session (FloraHome / FlorAI)

_Updated 2026-09-29. Read this top-to-bottom; it is written so a fresh agent can
continue cold. The repo is the source of truth and everything here is pushed to
GitHub (`KoloTim/FloraHome`, branch `main`)._

## 0. TL;DR of where we are

- **Dashboard/API work is done and live** on the Pi: FlorAI rebrand, EN/DE/NL i18n,
  native graph dashboard (Grafana removed), AI plant-control tools, modular
  sensors API+UI, backend API-key management, kiosk + onboarding.
- **Node stability**: the real cause of the "offline after ~90 s" was the firmware
  **re-processing the retained `estop` message continuously**, flooding the main
  loop and starving telemetry + MQTT keepalives. Fixed in firmware (`ce47e41`) and
  API (publish estop only on change). **plant-a verified stable**; plant-b flashed.
- **Network**: the Pi is **no longer an access point**. `wlan0` joined the home
  Wi-Fi **`Group1`** (Pi = `192.168.0.104`). Nodes also joined `Group1`; the MQTT
  broker address is a secret (`esphome/secrets.yaml: mqtt_broker`). The old
  `Hotspot` profile is saved as a rollback.
- **OPEN right now**: plant-a may need one clean reflash (last flashes were flaky —
  "chip stopped responding"); the Pi was resource-starved by parallel compiles.
  Reboot the Pi, then re-check both nodes.

## 1. Access

| Thing | Value |
|---|---|
| Pi (eth) | `192.168.91.68`, user `tim` / `timtimtim` |
| Pi (home Wi-Fi `Group1`) | `192.168.0.104` |
| Dashboard | `http://192.168.91.68:8098` (kiosk runs it) |
| API | `http://192.168.91.68:8097` |
| Stack dir | `/home/tim/smartplanter` (Docker Compose project `smartplanter`) |
| Nodes | `plant-a` = `plant-a` ZigbeeMQTT, `plant-b` |

SSH from this PC: **paramiko** (no `rsync` on Windows). Helper:
`C:\Users\User\AppData\Local\Temp\opencode\FloraHome\deploy\remote_exec.py`
run with env `RHOST/RUSER/RPW/RSCRIPT`. Work on the **GitHub Desktop clone**
`C:\Users\User\smartplanter`. Push with the GitHub token inline (never saved).

**Deploy pattern used all session** (write a bash script, run via RSCRIPT):
`git clone --depth 1` to `/tmp/fhrepo` on the Pi, `rsync` `api/ web/ deploy/
esphome/` into `/home/tim/smartplanter`, `docker compose up -d --build api web`,
then **`docker compose restart web`** (rsync `--delete` swaps the index.html
inode; nginx keeps the old one until restarted).

## 2. Critical Pi gotchas (learned this session)

- **Do NOT run two ESPHome compiles in parallel** — it starts resource-starvation
  and SSH stops responding. Compile serially.
- **ESPHome/flasher image**: pinned to `esphome/esphome:2025.8.4`; the `2025.8`
  layer was corrupt. The **`esphome` container cannot see `/dev`**; only
  `flasher` mounts `/dev`. Flash via a one-off:
  ```
  docker run --rm --privileged -v /dev:/dev -v /home/tim/smartplanter/esphome:/config \
    --entrypoint esptool esphome/esphome:2025.8.4 \
    --chip esp32 --port /dev/ttyUSBx --baud 115200 --after hard_reset \
    write-flash -z 0x0 /config/.esphome/build/<node>/.pioenvs/<node>/firmware.factory.bin
  ```
- **PlatformIO cache** in the esphome container got corrupt twice (missing
  `Arduino.h`/`esp_netif_types.h`). Fix: `rm -rf /root/.platformio/packages` inside
  the container, recompile (re-downloads, ~slow one time).
- **Kiosk white screen**: the Wayland socket is **`wayland-0`**; Chromium was told
  `wayland-1`. Fixed in `deploy/kiosk.sh` (auto-detects `$XDG_RUNTIME_DIR/wayland-*`).
  Kiosk relaunches itself if it exits.
- **SSH rate-limits**; retry with backoff (the helper does 6 tries). Avoid hammering.
- **`docker compose logs`** for mosquitto shows node disconnects: look for
  `disconnected: exceeded timeout` (= the estop-flood symptom).

## 3. What was built (all pushed)

1. **Node resilience** (`api/app.py`, `esphome/*.yaml`):
   - `node_fresh()` is time-based (config `node_offline_sec`, default 90 s); never
     gated by the LWT flag.
   - LWT handled: an explicit non-retained `offline` marks the node down; retained
     replays don't resurrect/flap.
   - **estop handler is idempotent** (`if (stop_all == id(halted)) return;`) — the
     flood fix. API publishes retained estop only on change.
   - `api.reboot_timeout: 0s`, `wifi.power_save_mode: NONE`, `mqtt.keepalive: 10s`,
     DHT `setup_priority: -100` + 60 s.
   - subscribe `planter/+/event`; `publish()` checks rc; rules gate on fresh data.
2. **FlorAI** (was "Flori"): floating animated mascot, contextual tips, drag/tap,
   chat at the top of Übersicht. Brand: *"FloraHome — AI-powered smart plant
   dashboard"*.
3. **AI plant control** (safety-first): tools `get_plant_state`, `set_watering`,
   `set_light`, `apply_species_care`, `water_now`, `write_diary`; per-plant
   `ai_control` = `off|ask|auto` (default off). Hard rails: offline/EMERGENCY-STOP
   refuse, dose clamp, daily cap, audit.
4. **i18n EN (default) / DE / NL** — 324+ keys, header switcher (was unwired, now
   fixed: `initLang()` + `#langSelect.onchange`).
5. **Graph dashboard** (`Verlauf` tab), **Grafana removed**.
6. **Modular sensors**: `/api/sensors/{node}` + `sensors` in `/api/state`; per-node
   `sensor_<key>_enabled` (tri-state; auto-detect from telemetry/fault). Backend
   tab renders cards with GPIO + wiring guide.
7. **Backend**: API-key management (runtime overrides in `meta`, `_reload_secrets`),
   System & status card.
8. **UX**: in-app confirm/error dialogs, onboarding checklist, resistive-touch
   polish, shorter/cuter diary entries.
9. **Kiosk**: resilient launcher + labwc autostart + systemd user watchdog.

## 4. TODO — continue here

### P0 (finish the node story — do first)
1. **Reflash plant-a cleanly** (it may still be on an older/corrupt image). One
   compile at a time; verify serial shows `Connected` + `mqtt:309`.
2. **Verify both nodes stay online 5+ min** on `Group1`.
3. See the hardware TODO below.

### P1 (user's explicit wishes not yet finished)
4. **Sensor troubleshooting UI** — guided per-sensor diagnosis (raw values,
   fault → "wire X to GPIO Y", step-by-step). The modular-sensor cards are the
   base; add a "Troubleshoot" flow.
5. **FlorAI even more present/interactive** — quick-action chips (e.g. "Water now",
   "Set up for species", "How is it?"), proactive tips, maybe a small avatar badge.
6. **Nicer plant switching** — replace/upgrade the header dropdown (tiles/avatars).
7. **Voice end-to-end test** — browser mic needs **HTTPS or localhost**. Plan:
   self-signed HTTPS reverse proxy on the Pi, or a `.local`/Tailscale hostname
   browsers treat as secure. Server side (`/api/voice/stt`, browser-first +
   Pi fallback, `play_on_pi`) is done and deployed.
8. **Charts refinement** — nicer axes/legend, maybe area smoothing, per-node
   overlay.

### P2 (hardware / polish)
9. **Wire plant sensors properly**: DHT11 needs a **10 kΩ pull-up** (GPIO27);
   plant-b currently `fault: dht11`. Soil probe calibration (plant-a reads 100 %).
10. **Repo polish**: README screenshots, `docs/` refresh (FlorAI, home-Wi-Fi,
    no-Grafana, modular sensors), tag a release.

## 5. Hardware / wiring reference (ESP32)

| Function | GPIO | Notes |
|---|---|---|
| Soil ADC | 34 | ADC1, input-only; power-gated by GPIO25 |
| Soil power | 25 | on only while sampling |
| DHT11 data | 27 | **10 kΩ pull-up to 3.3 V required** |
| LDR divider | 35 | ADC1, input-only |
| Relay pump | 26 | active-low (config `relay_inverted`) |
| Relay light | 13 | |
| Buzzer | 14 | |
| Button | 4 | to GND |

Serial: `/dev/ttyUSB0` = plant-a, `/dev/ttyUSB1` = plant-b.

## 6. Config keys (per node, `/api/config/node/<node>`)
`pump_auto`, `pump_threshold_pct`, `pump_seconds`, `pump_cooldown_min`,
`pump_max_per_day`, `alert_dry_pct`, `alert_dry_min`, `alert_silent_min`,
`alert_tank_pct`, `light_auto`, `light_on_below_lux`, `telegram_enabled`,
`buzzer_enabled`, `node_offline_sec`, `diary_enabled`, `diary_interval_days`,
`ai_control`, `sensor_<soil|dht|lux|tank|battery>_enabled`.

## 7. Secrets
`esphome/secrets.yaml` on the Pi (gitignored): `wifi_ssid: "Group1"`,
`wifi_password`, `mqtt_broker: "192.168.0.104"`, mqtt creds, ota/ap passwords.
Runtime API keys (AI, Influx, Telegram, host helper) are editable in
**Backend → API keys** (stored in the `meta` table; override `.env`).

## 8. First actions for the new session
```bash
# 1. Reach the Pi
RHOST=192.168.91.68 RUSER=tim RPW=timtimtim python remote_exec.py 'uptime; \
  curl -s localhost:8097/api/state | python3 -m json.tool | head -40'
# 2. If a node is missing, reflash it (serial-compile, one at a time) — see §2.
# 3. Continue the TODO list from P1 upward.
```
