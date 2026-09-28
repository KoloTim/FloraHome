# Status — live build log

_Last updated: 2026-09-28 (evening)._

## Done

- **Pi** (`Tim`, `192.168.91.68`): full stack running on a **32 GB card** (cloned
  from the 15 GB card). Dashboard `:8098`, API `:8097`, Grafana `:3030`,
  InfluxDB `:8086`, Mosquitto healthy.
- **Repo published:** https://github.com/KoloTim/FloraHome
- **Node #1** (ESP32 `5c:01:3b:be:98:f4`): flashed **firmware 1.2.0** — DHT11,
  LDR on GPIO35, HW-390 soil, no tank sensor.
- **OTA is enabled.** Node #1 now joins the **Pi's own 2.4 GHz hotspot
  `FloraHome`** (`10.42.0.10`), so its web UI (`:80`) and OTA (`:3232`) are
  reachable again. `esphome upload smartplanter.yaml --device 10.42.0.10`
  succeeds over Wi-Fi (~21 s, no USB).
- **ESPHome web dashboard** running at `http://192.168.91.68:6052`
  (compose profile `tools`) for browser-based OTA.
- **Pi display kiosk**: Chromium full-screen on the 800×480 HDMI panel,
  auto-started via `~/.config/autostart/planter-kiosk.desktop` +
  `deploy/kiosk.sh`; verified after a reboot.
- Fixed API alias bug (`api/app.py`): relay field `light` no longer clobbers
  numeric `lux`.

## Current sensor state (firmware 1.2.0)

| Reading | Value | Verdict |
|---|---|---|
| `lux` (LDR, GPIO35) | ~4000 | ✅ working |
| `temp_c` / `humidity` (DHT11, GPIO27) | ~22 °C / ~73 % | ✅ working |
| `moisture_pct` (HW-390, GPIO34) | 0–100 | ⚠️ uncalibrated (`soil_v ≈ 2.6 V`) |
| `rssi` | ~−40 | ✅ |
| `fault` | `ok` | ✅ |

## Next actions

1. **Calibrate the HW-390** — `bash deploy/calibrate.sh soil`, or send
   `{"action":"cal","soil_dry_v":X,"soil_wet_v":Y}` on `planter/cmd`. Runtime
   calibration persists on the node; no reflash.
2. **Wire the buzzer + relay/pump** — see `WIRING_FOR_DUMMIES.md` (Actuators).
3. Then Phase C bench safety checks (bucket of water) and Phase E polish.

## Open issues

- **Pi touchscreen not detected** — `lsusb` shows only a hub + the CP210x
  (ESP32); there is no HID touch device and nothing on I²C. The display's USB
  **touch cable must be connected** before touch can work. Then Chromium needs
  `--touch-events=enabled` and, if the axes are rotated, a libinput calibration
  matrix.
- **Node #2** (ESP32 `e0:8c:fe:e5:82:f4`) **not flashed** — needs the
  multi-node rework first (absolute `planter/cmd` would water both plants).
- No JSN-SR04T → tank feature intentionally absent.

## Operations (on the Pi)

```bash
# OTA from the Pi CLI (no USB):
cd ~/smartplanter/esphome && esphome upload smartplanter.yaml --device 10.42.0.10

# OTA from a browser on the LAN:
#   http://192.168.91.68:6052   (login: ADMIN_USER / ADMIN_PASSWORD from .env)

# kiosk:
sudo systemctl restart lightdm        # or just reboot; kiosk comes up by itself
```
