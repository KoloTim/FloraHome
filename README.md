# 🌱 FloraHome — Euregio Smart Garden Hackathon

Self-watering planter with live dashboard, history, alerts and an audit log.
Runs entirely on your own hardware — no cloud, no vendor account.

**Tested and working** on the devbox. The same files lift onto the Raspberry Pi unchanged.

> **Deployed?** Read [`DEPLOYMENT_HANDOFF.md`](DEPLOYMENT_HANDOFF.md) — it records the
> real Pi (`192.168.91.68`) + ESP32 deployment, the exact commands, and every trap
> we hit (NATed Wi-Fi, `data/` ownership, esptool stub, SD-card cloning).
> For the on-Pi overview screen see [`docs/PI_DISPLAY.md`](docs/PI_DISPLAY.md).
> Hands-on breadboard wiring, one sensor at a time:
> [`docs/WIRING_FOR_DUMMIES.md`](docs/WIRING_FOR_DUMMIES.md).

---

## What's in the box

| Container | Role | Port |
|---|---|---|
| `planter-mosquitto` | MQTT broker, authenticated | 1883 |
| `planter-influxdb` | time-series history | 8086 |
| `planter-telegraf` | MQTT → InfluxDB writer | – |
| `planter-api` | rules, alerts, audit log, history API, SSE | 8097 |
| `planter-web` | dashboard + reverse proxy (nginx) | 8098 |
| `planter-grafana` | bonus: stock Grafana dashboard | 3030 |
| `planter-esphome` | optional: flash/edit the ESP32 from the browser | 6052 |

Data path: **ESP32 → MQTT → { Telegraf → InfluxDB → Grafana }** and **→ API → SSE → dashboard**.
The API also posts to Telegram and exposes a Prometheus endpoint so the existing
kololab Prometheus can scrape the planter.

---

## Start here

```bash
cd /opt/stacks/smartplanter
cp .env.example .env  &&  nano .env      # fill in the CHANGE_ME values
./scripts/start.sh                        # builds, starts, smoke-tests, prints URLs
```

| | URL |
|---|---|
| Dashboard | http://&lt;host&gt;:8098 |
| API docs | http://&lt;host&gt;:8097/docs |
| Grafana | http://&lt;host&gt;:3030 |
| InfluxDB | http://&lt;host&gt;:8086 |

### Test before the hardware exists

```bash
./scripts/fake_node.sh              # one DRY reading -> triggers pump + alert logic
./scripts/fake_node.sh --wet        # one healthy reading
./scripts/fake_node.sh --loop       # a reading every 10 s, moisture drifting down
./scripts/fake_node.sh --fault      # simulate a dead DHT22
./scripts/seed_demo_data.sh         # 24 h of plausible history, so the chart is never empty
```

> Run `./scripts/fake_node.sh` once **before** the demo, so the chart has shape.

### Flash the ESP32

```bash
cd esphome
cp secrets.yaml.example secrets.yaml && nano secrets.yaml   # wifi + MQTT password
esphome run smartplanter.yaml                                # first time: USB
esphome run smartplanter.yaml --device <node-ip>             # after that: OTA
```

Or skip the CLI: `docker compose --profile tools up -d`, then open `http://<host>:6052`
and edit + flash from the browser. OTA works from there too — no USB after the first flash.

---

## Wiring

| Function | ESP32 pin | Notes |
|---|---|---|
| Soil moisture | GPIO **34** | **ADC1 only.** ADC2 is dead while Wi-Fi is on |
| Soil probe power | GPIO 25 | probe powered only while sampling (anti-corrosion) |
| DHT22 data | GPIO 27 | 10 kΩ pull-up to 3.3 V |
| BH1750 | GPIO 21 / 22 | I²C SDA / SCL |
| JSN-SR04T trig | GPIO **16** | |
| JSN-SR04T echo | GPIO **17** | **via level shifter** — echo is 5 V, ESP32 is 3.3 V |
| Relay IN1 (pump) | GPIO 26 | **10 kΩ pull-**down** to GND** — stops boot-time chatter |
| Relay IN2 (grow light) | GPIO 13 | |
| Buzzer | GPIO 14 | |
| Button "water now" | GPIO 4 | to GND, uses internal pull-up |

### Power — read this before connecting anything

- **The pump is 3–6 V DC. Do not switch 12 V into it.** The 12 V rail feeds the grow
  light only. Use a **separate** DC-DC converter (5 V, ≥2 A) for the pump.
- One LM2596 is not enough for ESP32 + relay + pump. Use two converters:
  one 5 V for logic, one 5 V/2 A+ for the pump.
- **Relay coils draw ~70–90 mA.** An ESP32 GPIO is rated for ~12–40 mA.
  Drive the relay through a transistor/MOSFET, or expect random reboots.
- **Never put 12 V on a breadboard.** Screw terminals for 12 V and the pump.
- The Pi 4 wants its own 5 V/3 A USB-C supply. Underpowering it corrupts the SD card.

### First-boot calibration (do not skip)

`moisture_pct` is worthless until you set two numbers in `esphome/smartplanter.yaml`:

1. Probe in **air**, watch `Bodenfeuchte Rohspannung` → set `soil_dry_v` (≈ 3.1–3.3 V)
2. Probe in a **glass of water** → set `soil_wet_v` (≈ 1.2–1.5 V)
3. Same idea for the tank: `tank_full_cm` (tank full) and `tank_empty_cm` (tank empty)

> The **JSN-SR04T has a ~25 cm blind zone.** If your tank is shallower than that, it
> returns nothing useful — swap it for a float switch. Measure your tank first.

---

## MQTT topics

| Topic | Direction | Payload |
|---|---|---|
| `planter/telemetry` | node → broker | JSON, see below |
| `planter/cmd` | API → node | `{"action":"pump","seconds":20}` · `light` · `buzzer` · `stop` |
| `planter/state/<field>` | API → broker (retained) | one value per topic, for Home Assistant |
| `planter/status` | node → broker | `online` / `offline` (LWT) |

Telemetry payload:

```json
{"device":"smartplanter","fw":"1.0.0","ts":1790595253,
 "moisture_pct":19.4,"temp_c":21.6,"humidity":57,"lux":8300,
 "tank_pct":78,"rssi":-58,"pump":"idle","mode":"auto","fault":"ok"}
```

`fault` is a comma-separated list of sensors that returned `NaN` (`"soil"`,
`"dht22"`, `"bh1750"`, `"jsn_sr04t"`), or `"ok"`.

---

## Safety design (the bit that matters)

The split is deliberate:

- **The ESP32 enforces safety.** The pump relay is `ALWAYS_OFF` on boot, the pump
  script is `mode: single` so a second command cannot extend a running dose, and the
  run time is clamped to `pump_max_seconds` (45 s) **in firmware**.
  **If the Raspberry Pi dies mid-watering, the relay still opens.**
- **The Pi provides comfort.** When to water, when to shout, what to log.

So the failure mode of a crashed Pi is "the plant goes thirsty", never "the plant drowns".

Additional guards: cooldown between doses, max doses per day, pump blocked when the
tank is low, and a dry-soil alert that keeps re-raising until moisture actually rises.

### Alerts (Telegram, 10-minute anti-spam per code)

| Code | Trigger |
|---|---|
| `node_silent` | no telemetry for `alert_silent_min` (default 5 min) |
| `soil_dry` | moisture below `alert_dry_pct` for `alert_dry_min` |
| `tank_empty` | tank below `alert_tank_pct` |
| `sensor_fault` | node reports a sensor as faulty |

> A dead sensor cannot be reported **by the sensor** — that is why `node_silent` and
> `sensor_fault` are raised by the Pi, not by a local buzzer. The buzzer only covers
> things the node can still see.

---

## Security (CORE requirement)

- The dashboard is **read-only** until you log in. `GET` endpoints are open,
  every `PUT`/`POST` requires an HMAC-signed session cookie (12 h).
- Login is rate-limited: 10 failures from one IP → 5-minute lockout.
- **Every** config change, manual command, login, failure, alert raise and alert clear
  is written to the `audit` table with actor, IP and timestamp.
- Anonymous Grafana is **Viewer only** (`GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer`) —
  the public view can look but not touch.

```bash
# see who changed what
curl -b cookies.txt http://localhost:8097/api/audit | python3 -m json.tool
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `unable to open database file` | `./data` not writable by `PUID`. Check `PUID`/`PGID` in `.env` |
| Mosquitto restart-looping, `Unable to open pwfile` | stale root-owned `passwd` in the volume: `docker compose down && docker volume rm smartplanter_mqtt-data && docker compose up -d` |
| Telegraf `no such host mosquitto` | it started before the broker was healthy; `docker compose restart telegraf` |
| Pump runs at boot | relay is active-LOW → set `relay_inverted: "true"`, and fit the 10 kΩ pull-down |
| Moisture always 0 % or 100 % | wrong pin (must be ADC1) or uncalibrated `soil_dry_v` / `soil_wet_v` |
| Tank reads `nan` | object closer than the 25 cm blind zone, or echo pin needs the level shifter |
| Chart empty | run `./scripts/seed_demo_data.sh` |

Ports already taken on the devbox: `1883` free, but Grafana needed `3030`
(the devbox already runs Grafana on 3003). All ports are set in `.env`.

---

## Moving to the Raspberry Pi

The whole directory is designed to be copied as-is:

```bash
# on the devbox
rsync -av --exclude data --exclude '.esphome' \
      /opt/stacks/smartplanter/ pi@<pi-ip>:/home/pi/smartplanter/

# on the Pi
cd ~/smartplanter && cp .env.example .env && nano .env   # change PUID/PGID to 1000
docker compose up -d --build
```

On the Pi, also set `mqtt_broker` in `esphome/smartplanter.yaml` to the **Pi's** IP
(it currently points at the devbox), then re-flash OTA.

For the demo: `restart: unless-stopped` is already set on every service, so the
stack comes back by itself after a power cut.
