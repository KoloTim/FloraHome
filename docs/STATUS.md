# Status — live build log

_Last updated: 2026-09-28._

## Done

- **Pi** (`Tim`, `192.168.91.68`): full stack running on a **32 GB card** (cloned
  from the 15 GB card). Dashboard `:8098`, API `:8097`, Grafana `:3030`,
  InfluxDB `:8086`, Mosquitto healthy.
- **Repo published:** https://github.com/KoloTim/FloraHome
- **Node #1** (ESP32 `5c:01:3b:be:98:f4`, /dev/ttyUSB0): flashed **firmware
  1.1.0** — DHT11, LDR on GPIO35, HW-390 soil, no tank sensor.
- Live telemetry to the Pi every 10 s (`device: smartplanter`).
- Fixed API alias bug (`api/app.py`): relay field `light` no longer clobbers
  numeric `lux`.
- Pi display plan + kiosk script (`PI_DISPLAY.md`, `deploy/setup-kiosk.sh`).

## Current sensor state (firmware 1.1.0)

| Reading | Value | Verdict |
|---|---|---|
| `lux` (LDR, GPIO35) | ~679 | ✅ working |
| `moisture_pct` (HW-390, GPIO34) | 100 | ⚠️ uncalibrated / check wiring |
| `temp_c` / `humidity` (DHT11, GPIO27) | absent | ❌ `dht11` in `fault` |
| `rssi` | ~−62 | ✅ |
| `fault` | `dht11` | – |

## Next actions

1. **Fix the DHT11** — `[W][dht:050]: Invalid readings! Check pin number and
   pull-up resistor.` Wire DATA→**GPIO27**, VCC→3V3, GND→GND, **10 kΩ DATA↔3V3**
   (many modules lack it). Confirm it's a DHT11 (firmware model).
2. **Check the HW-390** — AOUT→GPIO34, VCC→GPIO25, GND→GND. `moisture_pct` 100
   with an uncalibrated probe is expected; calibrate in air vs tap water.
3. **Next firmware build** adds **raw `soil_v` and `ldr_v` to telemetry** so
   calibration can be done over MQTT (OTA is unavailable behind the NAT).
4. Then Phase C (relay + 12 V pump, bucket of water) and Phase E (Pi display).

## Open issues

- **`Group1` is NATed** → node web UI + OTA unreachable. USB flashing for now.
- **Node #2** (ESP32 `e0:8c:fe:e5:82:f4`, /dev/ttyUSB1) **not flashed** — needs
  the multi-node rework first (absolute `planter/cmd` would water both plants).
- No JSN-SR04T → tank feature intentionally absent.
