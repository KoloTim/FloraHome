# Handoff — next session (FloraHome / FlorAI)

_Updated 2026-09-29. Read this top-to-bottom; it is written so a fresh agent can
continue cold. The repo is the source of truth and everything here is pushed to
GitHub (`KoloTim/FloraHome`, branch `main`)._

## 0.5 Update — later 2026-09-29 session (OTA-only + UX)

- Pi rebooted and **healthy**; all 7 containers up. Both nodes online on
  `Group1`: `plant-a` = `192.168.0.105`, `plant-b` = `192.168.0.103`.
- **OTA-only works.** plant-a was reflashed over Wi-Fi, no USB:
  `docker exec planter-esphome esphome run /config/plant-a.yaml --device 192.168.0.105 --no-logs`
  (the `esphome` container is host-networked and has `/config` mounted; only the
  `flasher` container needs `/dev`). USB cables are no longer needed day-to-day.
  Caveat: `plant-a` still dropped to offline ~90 s after boot once — watch it;
  possibly hardware / the missing DHT pull-up. `plant-b` is rock-solid.
- **Fixed a live bug**: `api_ai_chat` called `require_user(...)` without
  assigning, then used `user` → `NameError` 500 on every chat. Now assigned. The
  tool-chat loop also now falls back through `AI_FALLBACKS` like plain chat.
- **New UX (deployed)**: plant-switching **tiles**; FlorAI **quick-action
  chips**; guided **per-sensor troubleshooting** dialog; **charts polish**
  (gradient area, last-point dot, unit label, vertical grid).
- **New plant flow**: `📷 New plant` = name + phone photo → `POST /api/ai/identify`
  now returns a `proposed` profile (AI-proposed care stats, or the curated
  catalogue's values when the species matches) → one tap creates the profile
  (care is stored on the plant row, `_smart_overrides` applied).
- **Sensors API** now returns `status / raw / unit / cause / checks` from
  `sensors_for()` (feeds the troubleshooting UI).
- **Smart watering by VOLUME (flow-rate dosing)**: new per-node config
  `pump_ml_per_s` (default 30 → R385 ≈ 25-33 mL/s at 12 V) and `pot_ml`
  (substrate water capacity, default 1500 mL). Auto-watering now computes the
  dose: `seconds = (threshold − moisture)/100 × pot_ml ÷ pump_ml_per_s`
  (`dose_seconds()`), clamped to `pump_max_seconds` (45). The AI tools accept
  `ml_per_s`/`pot_ml` (`set_watering`) and `ml` (`water_now`). Editable in
  Control → ⚙️ Watering. **Tune `pot_ml` per pot** — it is the knob that makes
  the dose match the plant type/pot size.
- **Kiosk autostart FIXED**: the launcher had lost its `+x` bit (rsync), so the
  labwc autostart's direct exec silently failed. Now every hook invokes
  `/bin/bash …/kiosk.sh` and `setup-kiosk.sh` strips duplicate launchers + chmods
  the script. If the screen is ever blank at a kiosk, check
  `~/.config/planter-kiosk/kiosk.log`.
- **Easter eggs + sensor reuse**: FlorAI now understands
  "party/dance", "rain/confetti", "sing/song", "love" and "konami" in chat
  (client-side, works even when the AI is busy); plants celebrate their profile
  birthday; the floating tips use the LDR (`<30 lx` → good night, `>60k lx` →
  sunbathing), temperature (too cold) and battery (`<20%`). Reader ideas below.
- **Sensor-reuse ideas** (all already wired on the ESP32, no new hardware):
  `LDR` (GPIO35) → day/night, "wave to say hi" (sharp lux dip), plant
  sun-tracking notes; `buzzer` (GPIO14) → friendly beeps/  "FlorAI sings"
  (needs fractional-second beeps in firmware first — currently whole seconds via
  `{"action":"buzzer","seconds":N}`); `button` (GPIO4) → secret tap patterns
  (e.g. 5 taps = water + cheer); `DHT11` → seasonal greetings; `RSSI` → "signal
  strength" game; `soil_v` raw → a "how wet is it really" calibration game.
- Known gaps: AI provider returned `503 high demand` during testing (retry
  later); `ai_control: "ask"` still behaves like `auto` (no approval step yet);
  `plant-a` DHT11 `fault: dht11` until the 10 kΩ pull-up is fitted.

## 0.7 Presentation-ready state (latest)

- **Login DISABLED** (`AUTH_DISABLED`, default on) — every control works without
  signing in; the Sign‑in button is hidden. Set `AUTH_DISABLED=0` to re‑enable.
- **Single Demo toggle** (Backend → 🎬 Demo mode): one button fills the dashboard
  with 5 example plants — varied readings/moods, bullet diaries, 36 h history
  charts and a few notifications. Persists across restarts (`meta.demo_enabled`).
- **Profile pictures** per plant: generated species avatars for demo plants;
  real plants get a photo in **Edit profile** (> resized client‑side); used on the
  plant cards and the top switching tiles.
- **Water dosing fixed**: `pot_ml` 400, `pump_max_ml` 120 (hard per‑dose cap),
  `pump_max_seconds` 20. Formula: `min(deficit%·pot_ml, pump_max_ml) ÷ pump_ml_per_s`.
- **Diary** = 3 clean bullets per entry (old paragraphs auto‑normalised on read).
- **Sensors**: the Sensors card shows only sensors that actually report; a
  “Show all” toggle reveals the rest. Board list filters out templates
  (`battery-template`) → only `plant-a/b/c`.
- **Kiosk hardened**: `deploy/kiosk.sh` clears stale `Singleton*` locks, disables
  Chromium background networking (fewer network‑service crashes) and runs a
  **watchdog** on DevTools `:9222` that restarts Chromium if the tab ever lands on
  an error/blank page. (A hostname change or ethernet unplug had left it white.)
- **Hostname** is `florahome` → `http://florahome.local:8098/` works (Avahi).
- Pi is **Wi‑Fi‑only** at `192.168.0.104` (reservation). A failing SD card has
  caused spontaneous reboots + dockerd corruption; **back up / replace the card**.

## 0.6 INCIDENT — SD card is failing (read this first!)

Two separate failures landed at once:

1. **Dashboard "nothing clickable"** — self-inflicted: I called `renderChatChips()`
   from `applyI18n()` at page load, but it reads the `const CHAT_CHIPS` declared
   *later* in the script → `ReferenceError` (temporal dead zone) aborted the whole
   script, so no handlers were bound. Fixed: the call now happens after
   `CHAT_CHIPS` is initialised (and from `setLang`). If the UI is ever dead again,
   open the browser console and look for `ReferenceError` / `Cannot access … before
   initialization`.
2. **The Pi rebooted on its own and `dockerd` would not start** —
   `fatal error: invalid function symbol table` / `pcHeader: magic=0x0`
   (corrupt binary). Reinstalled with
   `sudo apt-get install -y --reinstall docker-ce docker-ce-cli` → daemon OK.
   **BUT** the containerd content store is still corrupt:
   `docker images` and any `docker pull` fail with
   `children: invalid desc sha256:e49acaf2…: invalid character '\x00'`.
   Running containers are fine and the stack is up, but **you cannot rebuild or
   pull images** until this is cleared.

**This is a dying SD card** (it also corrupted the dockerd binary). Strongly
recommended: **clone to / re-image a new card**. Until then, avoid
`docker compose up --build` / `docker pull`; static `web/` updates still work
because nginx mounts `./web`.

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
