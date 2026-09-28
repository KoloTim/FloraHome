# Bring-up plan — from bare node to full demo

Ordered, verifiable stages. Each stage ends with a **pass criterion**. Do not
start the next stage until the current one passes — that is how you keep a
failure attributable to one wire/sensor.

Current state (see `../DEPLOYMENT_HANDOFF.md`): Pi stack healthy, ESP32 online,
`fault = dht22,bh1750,jsn_sr04t,`, nothing wired.

---

## Verification toolkit (use after every change)

```bash
# on the Pi
python3 ~/node_log.py 25            # ESP32 serial: boot banner + sensor + MQTT lines
cd ~/smartplanter && set -a && . ./.env && set +a
docker run --rm --network smartplanter_default eclipse-mosquitto:2 \
  mosquitto_sub -h mosquitto -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t 'planter/telemetry' -v
curl -s http://localhost:8097/api/state | python3 -m json.tool
```

`fault` is a comma-list of sensors returning NaN. It is your compass: when a
sensor is wrong, it names it.

---

## Phase A — Sensors, one at a time

> Unplug the ESP32's USB while changing wiring. Replug to test. 3.3 V logic only
> on the ESP32 pins (JSN-SR04T echo excepted — level shifter).

### A1. DHT22 (temperature + humidity)
- **Wire:** VCC→3.3 V, GND→GND, DATA→**GPIO 27**, 10 kΩ DATA↔3.3 V unless the
  module already has one. Bare 4-pin: 1=VCC, 2=DATA, 3=NC, 4=GND.
- **Expect (serial):** `[I][dht:xxx]: Temperature 21.6°C, Humidity 57%` every 30 s.
- **Pass:** `temp_c` & `humidity` present in telemetry; `dht22` gone from `fault`.
- **Fail signs:** `[W][dht:050]: Invalid readings!` → missing pull-up or wrong pin.

### A2. BH1750 (light, lux)
- **Wire:** VCC→3.3 V, GND→GND, SDA→**GPIO 21**, SCL→**GPIO 22**.
- **Pass:** `[I][i2c.arduino:099] Results from bus scan` shows a device, and
  `lux` appears in telemetry; `bh1750` gone from `fault`.
- **Fail signs:** `Found no devices` → swap SDA/SCL, check 3.3 V, or the module's
  address (config expects `0x23`; some boards strap `0x5C` — that is a config
  change + reflash).

### A3. Soil moisture probe
- **Wire:** probe signal→**GPIO 34**, probe power→**GPIO 25**, GND→GND.
  **GPIO 34 is ADC1 — never move it to an ADC2 pin (ADC2 dies while Wi-Fi is on).**
- **Expect:** `Bodenfeuchte Rohspannung` = a raw voltage that changes when the
  probe is in air vs water. `moisture_pct` is **meaningless until calibrated**.
- **Pass:** raw voltage is a believable number (not stuck at 0 or 3.3 forever).
- **Note:** capacitive probes want **tap water** for the wet point, not distilled.

### A4. JSN-SR04T (tank level)
- **Wire:** VCC→5 V, GND→GND, TRIG→**GPIO 16**, ECHO→**level shifter**→**GPIO 17**.
  The echo line is 5 V; the ESP32 pin is **not 5 V tolerant**.
- **Pass:** `Abstand Wasseroberfläche` reads a sane cm distance; `tank_pct` appears.
- **Fail signs:** `nan` → object closer than the ~25 cm blind zone, echo not
  shifter-protected, or TRIG/ECHO swapped.

**Phase A pass:** `fault = ok` and all five live values sane.

---

## Phase B — Calibration (do NOT skip)

`moisture_pct` and `tank_pct` are lies until these two-point calibrations exist.
All are `substitutions` in `esphome/smartplanter.yaml`, so each change is a
**rebuild + flash** (see `../DEPLOYMENT_HANDOFF.md` §4).

| Field | Measure | Set |
|---|---|---|
| `soil_dry_v` | probe in **air**, read raw V | ______ |
| `soil_wet_v` | probe in **glass of tap water** | ______ |
| `tank_full_cm` | sensor→water surface, tank **full** | ______ |
| `tank_empty_cm` | same, tank **empty** | ______ |

Formula in firmware: `pct = (dry_v - measured_v) / (dry_v - wet_v) * 100`.

**Phase B pass:** dry air → ~0–10 %, water → ~90–100 %; tank full → ~100 %,
empty → ~0 %.

> ⚠️ **OTA is blocked** by the `Group1` NAT, so every firmware change needs the
> off-box build + **USB** flash. If calibration flashes get tedious, move the
> node to a flat 2.4 GHz network on `192.168.91.0/24` (then OTA works). Decide
> this before Phase B.

---

## Phase C — Relay + pump (HUMAN HANDS, bucket of water)

> Never run the pump dry. Never put 12 V to the pump. Power off while wiring.

### Power architecture
| Bus | Supplies | Current | Notes |
|---|---|---|---|
| 5 V **logic** | ESP32 + relay coils | ~1.2 A | one LM2596 |
| 5 V **pump** | pump only, **separate buck** | ≥2 A | inrush kills weak supplies |
| 12 V | grow light only | — | **2 A inline fuse. Never to the pump.** |

- Relay coil 70–90 mA > GPIO 12–40 mA → drive via **transistor/MOSFET**, not the
  pin. 10 kΩ pull-down on **GPIO 26** (pump) stops boot chatter.
- Determine active level: IN pin reads 3.3 V at rest → **active LOW** →
  `relay_inverted: "true"`. Pump must **not** twitch on boot.

### Bench tests
1. Pump via the relay's **manual/test button** first (isolates pump vs wiring).
2. Pump submerged in a bucket; time flow (`___ mL / 20 s`) → set real dose.
3. Run 60 s by hand; nothing warm.

### Firmware safety checks (the bit judges care about)
- Hold **"Wasser jetzt"** (GPIO 4) 60 s → pump stops on its own at **45 s**.
- Pull ESP32 power mid-dose → pump off; power back → still off.
- Trigger **NOT-AUS** from the dashboard → pump+light off, auto-watering blocked
  even at 5 % moisture. Clear → works again.

**Phase C pass:** all three safety checks behave, and `pump` shows
`watering`/`idle` in telemetry.

---

## Phase D — Full loop + history

1. Let moisture fall below `pump_threshold_pct` (or force it) → auto dose fires,
   clamped ≤45 s.
2. Confirm `pump` rows reach InfluxDB (string field), Grafana shows the curve.
3. Telegram alert arrives (cooldown 600 s — a second alert inside 10 min is
   suppressed, expected).

**Phase D pass:** the four acceptance rows in `../DEPLOYMENT_HANDOFF.md` §8
(telemetry, Influx pump, e-stop, audit) all green.

---

## Phase E — On-Pi display

See `PI_DISPLAY.md`. Default: kiosk browser on `http://localhost:8098/`, enabled
with `deploy/setup-kiosk.sh` + desktop autologin.

---

## Phase F — More than one plant (only if needed)

The stack is **single-node** today (one global STATE, absolute MQTT topics). A
second ESP would share `planter/cmd` and *both would water*. Before a second
node:

1. Firmware → per-node topics `planter/<node>/…`.
2. `api/app.py` → per-node state dict + per-node subscribe/publish.
3. Display → per-plant tiles.

Do **Phase A–E on one node first**; split to multi-node only once the single
node is solid.
