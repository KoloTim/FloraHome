# Smart Planter — Hardware Deployment Handoff (for an OpenCode session)

You are an agent on the user's PC. Your job: take the Smart Planter stack
(already built and verified on the devbox at `192.168.178.185`) and get it
running on **real ESP32 hardware**.

Read this whole file before running anything. Two hard rules up front.

---

## 0. Hard rules — do not violate these

1. **NEVER run the pump dry, and never run it outside a bucket of water.**
   The pump is a 3–6 V DC motor. Every pump test happens with the intake in
   water. There is no exception for "just a second".
2. **NEVER connect 12 V to the pump.** 12 V feeds the grow light only.
   The relay module switches the pump's own 5 V buck, nothing else.
3. **You must not bypass the firmware's pump ceiling.** `pump_max_seconds: 45`
   is enforced in firmware (below). Do not raise it to "test faster".
4. **Physical wiring is the human's job, not yours.** You write config, compile,
   flash, and verify over the network. If a wire needs moving, stop and ask.

If a step below requires a human hand (plugging in USB, moving a jumper),
say so explicitly and wait. Do not improvise around it.

---

## 1. Get the code onto the PC

**This is NOT a git repository.** There is no remote, no commits, no branch.
Do not try `git clone`. Copy the tree.

From the PC (adjust the destination):

```bash
rsync -av --exclude 'esphome/.esphome' --exclude 'data' --exclude 'esphome/secrets.yaml' \
  kolotim@192.168.178.185:/opt/stacks/smartplanter/ ~/smartplanter/
```

`--exclude esphome/.esphome` skips several hundred MB of build cache.
`--exclude esphome/secrets.yaml` is deliberate — see §3.1, you create it fresh.

If `rsync` is unavailable, `scp -r` works but will drag the build cache along.

The PC must be on the **same L2 network** as the devbox (`192.168.178.0/24`),
because the broker lives there.

---

## 2. Prerequisites on the PC

You need ESPHome **2025.8** (the project pins `esphome/esphome:2025.8` and
sets `min_version: "2025.8.0"`). Two options:

**Option A — pipx (recommended if the ESP32 is plugged into the PC):**

```bash
pipx install esphome==2025.8
esphome version
```

**Option B — Docker (matches `scripts/build_firmware.sh`):**

```bash
docker pull esphome/esphome:2025.8
```

Docker needs USB passthrough to flash, which on Linux means:

```bash
docker run --rm -it --device=/dev/ttyUSB0 -v "$PWD/esphome":/config \
  esphome/esphome:2025.8 run /config/smartplanter.yaml
```

On macOS/Windows the device node differs (`/dev/cu.usbserial-*`, `COM3`).
**Option A avoids this entire problem** — prefer it for the first USB flash.

Also confirm `python3` and `curl` exist; several verification steps use them.

---

## 3. Phase 1 — Fix the blockers before flashing (no hardware needed)

There are three real blockers. Fix all three before you touch the board.

### 3.1 `esphome/secrets.yaml` is a placeholder — firmware cannot join Wi-Fi

It currently contains `wifi_ssid: "TEST"`. Create a real one:

```bash
cd ~/smartplanter/esphome
cp secrets.yaml.example secrets.yaml
$EDITOR secrets.yaml
```

Fill in:

| Key | Value |
|---|---|
| `wifi_ssid` | The **2.4 GHz** SSID. ESP32 has no 5 GHz radio. |
| `wifi_password` | That network's password. |
| `ap_password` | Any string. Fallback AP is named `smartplanter fallback`. |
| `ota_password` | Any string. Used for all later over-the-air flashes. |
| `mqtt_username` | Must equal `MQTT_USER` in the devbox `.env` (currently `planter`). |
| `mqtt_password` | Must equal `MQTT_PASSWORD` in the devbox `.env`. |

**Get the MQTT password by reading the devbox `.env` — do not ask for it to be
pasted into a chat, and do not hardcode it into this file or any commit.**

```bash
ssh kolotim@192.168.178.185 'grep "^MQTT_PASSWORD=" /opt/stacks/smartplanter/.env'
```

`esphome/secrets.yaml` is gitignored (see `esphome/.gitignore`). Keep it that
way. Never commit it.

### 3.2 `mqtt_broker` must point at the right host

In `esphome/smartplanter.yaml`, substitutions block (~line 26):

```yaml
mqtt_broker: "192.168.178.185"   # devbox — correct for bench testing
```

This is already correct for testing against the devbox broker. **When you move
to the demo Pi, change this to the Pi's IP and reflash.** Flag it loudly to the
user at that moment.

### 3.3 The README pin map is WRONG — use the YAML

`README.md` "Wiring" table says JSN-SR04T on **GPIO 5 / 18**.
`esphome/smartplanter.yaml` substitutions say **GPIO 16 / 17**.

**The YAML is authoritative.** It deliberately avoids GPIO 5 (a strapping pin)
and wires echo to 17. The README is stale. **Wiring from the README gives a dead
tank sensor.** Either fix the README table or tell the user explicitly.

---

## 4. Phase 2 — Compile, then flash

### 4.1 Compile first (catches errors a config check cannot)

```bash
cd ~/smartplanter && ./scripts/build_firmware.sh
```

First run downloads the toolchain: **5–15 minutes**. Do not interrupt it.
This runs `esphome config` then `esphome compile`.

### 4.2 Flash over USB — first time only

**Human action required: plug the ESP32 into the PC via USB data cable**
(not a charge-only cable — a charge-only cable is a classic silent failure).

Wemos D1 mini ESP32 / ESP32-WROOM-32. If the port does not appear, the board
likely needs a USB-UART driver (CP2102 or CH340 depending on clone).

```bash
cd ~/smartplanter/esphome
esphome run smartplanter.yaml         # pick the serial port when prompted
```

All subsequent updates go over the air — **no USB ever again**:

```bash
esphome run smartplanter.yaml --device <node-ip>
```

### 4.3 Bench check with USB power only

**Before relays, before the pump, before anything else.** Power the ESP32 from
USB alone and verify:

- Node's own web UI responds: `curl -s -o /dev/null -w '%{http_code}\n' http://<node-ip>/`
  → `200` (the firmware serves `web_server` on port 80)
- MQTT is connected and telemetry flows (run from the devbox, or any host that
  has `mosquitto_sub`):

```bash
mosquitto_sub -h 192.168.178.185 -u planter -P "$MQTT_PASSWORD" -t 'planter/#' -v
```

You should see a retained-free JSON blob on `planter/telemetry` **every 10 s**,
and `planter/status` = `online` (birth message).

**Acceptance for Phase 2:** telemetry JSON arriving every 10 s, and this
returns the node's data:

```bash
curl -fsS http://localhost:8097/api/state | python3 -m json.tool
```
(run that on the devbox, where the API container lives)

---

## 5. Phase 3 — Sensors, one at a time

Add one sensor, confirm its value appears, move on. Do not wire everything at
once — you will not be able to attribute a failure.

**Watch values live in the node's own web UI at `http://<node-ip>/`.**

1. **DHT22** — data GPIO **27**, 10 kΩ pull-up to 3.3 V → `temp_c`, `humidity`
2. **BH1750** — I²C SDA GPIO **21**, SCL GPIO **22** → `lux`
3. **Soil probe** — signal GPIO **34**, probe power GPIO **25** → `moisture_pct` raw first
4. **JSN-SR04T** — trig GPIO **16**, echo GPIO **17** **via level shifter** → `tank_pct`

Two traps here:

- **GPIO 34 is ADC1. Never move the soil probe to an ADC2 pin (0/2/4/12/13/14/15/25/26/27).**
  ADC2 is unusable while Wi-Fi is on — it will read garbage or nothing.
- **JSN-SR04T echo is 5 V; the ESP32 pin is 3.3 V.** The level shifter is not optional.

The firmware reports a `fault` string naming any sensor reading NaN, e.g.
`"soil,dht22"`. Use it: if a sensor is miswired, the fault field tells you which.

---

## 6. Phase 4 — Relay and pump (human hands, bucket of water)

**Stop and hand this to the human.** Give them these instructions verbatim.

### Power architecture — the two-bus rule

| Bus | Supplies | Current | Notes |
|---|---|---|---|
| 5 V **logic** | ESP32 + relay coils | ~1.2 A | One LM2596 is enough |
| 5 V **pump** | pump only, separate buck | ≥2 A | Inrush on start is the killer |
| 12 V | grow light only | — | **2 A inline fuse. Never to the pump.** |

- Buck #1 → ESP32 5V pin. Confirm 5.0 V ±0.2 V **under load** (relay energised).
- Buck #2 (≥2 A) → pump. Pump V- and logic GND tied **at the buck, one point**.
- **Relay coils draw 70–90 mA. An ESP32 GPIO sources ~12–40 mA.** Drive the relay
  through a transistor/MOSFET, or expect random reboots mid-watering.
- **10 kΩ pull-down on GPIO 26** (pump relay) — stops boot-time relay chatter.
- Relay modules are usually **active LOW**. If the pump runs when idle, set
  `relay_inverted: "true"` in the substitutions block.

### Before involving the ESP32

Test the pump **using the relay's own manual/test button** first. This isolates
"is the pump fine" from "is my wiring fine".

### Firmware safety — already implemented, verify it

- `pump_max_seconds: "45"` — hard ceiling, no command can exceed it
- `restore_mode: ALWAYS_OFF` — pump is off on every boot
- `on_boot priority: 800` — forces pump/light/buzzer off before anything else runs
- The `script: pump_dose` enforces the ceiling in firmware, **not on the Pi**.
  If the Pi dies mid-watering, the relay still opens. That is the design.

### Test the command path

```bash
# watch what the API publishes
mosquitto_sub -h 192.168.178.185 -u planter -P "$MQTT_PASSWORD" -t 'planter/cmd' -t 'planter/estop' -v
```

Or press the physical **"Taste Wasser jetzt"** button (GPIO **4** to GND,
internal pull-up) — it runs a local 15 s dose and publishes
`{"event":"button_water_now"}` on `planter/event`.

---

## 7. Phase 5 — Calibration (do not skip)

`moisture_pct` is meaningless until these are measured. All in the substitutions
block of `esphome/smartplanter.yaml`:

```yaml
soil_dry_v: "3.10"    # probe in AIR        -> raw voltage (usually ~3.1–3.3)
soil_wet_v: "1.35"    # probe in GLASS WATER -> raw voltage (usually ~1.2–1.5)
tank_full_cm: "8.0"   # distance sensor->water surface, tank FULL
tank_empty_cm: "30.0" # same, tank EMPTY
```

Formula the firmware uses:
`moisture_pct = (soil_dry_v - measured_v) / (soil_dry_v - soil_wet_v) * 100`

**JSN-SR04T has a ~25 cm blind zone.** If the tank is shallower than ~25 cm it
will read nothing useful — the correct fix is a float switch, not more code.
Tell the user this rather than fiddling with thresholds.

After editing, reflash OTA: `esphome run smartplanter.yaml --device <node-ip>`

---

## 8. Phase 6 — Full loop + dashboards

Watch `moisture_pct` fall below the threshold (`ALERT_DRY_PCT=25` in `.env`).
Confirm, in order:

1. Auto-watering fires (pump runs, clamped to ≤45 s)
2. `pump` reaches InfluxDB (see §9 acceptance query)
3. Telegram alert arrives — **note the cooldown is 600 s**, so a second test
   inside 10 minutes will be silently suppressed. That is expected, not a bug.

Dashboards:

- **Web dashboard** — `http://192.168.178.185:8098/` (verified HTTP 200)
- **Grafana** — `http://192.168.178.185:3030/` (verified HTTP 200)
- **Node's own web UI** — `http://<node-ip>/`

---

## 9. Acceptance criteria

Every one of these should pass. Report the actual output, not "looks fine".

| # | Check | Command / where |
|---|---|---|
| 1 | Node web UI up | `curl -s -o /dev/null -w '%{http_code}\n' http://<node-ip>/` → 200 |
| 2 | Telemetry every 10 s | `mosquitto_sub ... -t 'planter/telemetry' -v` |
| 3 | Node registered | `curl -fsS http://localhost:8097/api/state` → `"online": true`, `last_seen_age_s` < 30 |
| 4 | No sensor faults | same output → `"fault": "ok"` |
| 5 | `pump` reaches InfluxDB | query below |
| 6 | E-stop suppresses watering | `cd /opt/stacks/smartplanter && ./scripts/test_estop.sh` on devbox → `✅ PASS` |
| 7 | Audit trail written | `curl -fsS -b /tmp/estop.jar "http://localhost:8097/api/audit?limit=5"` |

Check 5 — prove my one-line telegraf fix works with real hardware:

```bash
docker exec planter-influxdb influx query \
  "from(bucket:\"planter\") |> range(start:-10m) \
   |> filter(fn:(r)=>r._measurement==\"planter\" and r._field==\"pump\") \
   |> keep(columns:[\"_time\",\"_value\",\"device\"])" \
  --org kololab --token "$INFLUX_TOKEN"
```

Expect `watering` / `idle` rows. If `pump` is absent, `json_string_fields` in
`telegraf/telegraf.conf` lost the fix — it must read:

```toml
json_string_fields = ["fault", "mode", "pump"]
```

Note: the **firmware publishes `pump` automatically** as `"watering"` or
`"idle"` derived from `pump_relay.state`. Nothing needs to be added to the
firmware for this. It is a telegraf/Influx concern only.

---

## 10. Known landmines

1. **Global single-node state.** `api/app.py` holds ONE global `STATE` dict.
   **Do not run `scripts/fake_node.sh` while the real node is live** — the fake
   node (`device: "planetest"`) and the real one (`device: "smartplanter"`)
   will overwrite each other's data. Pick one.

2. **Per-node topics are NOT implemented.** In `esphome/smartplanter.yaml`:
   `topic_prefix: planter/${device_name}` is set, but `on_json_message` listens
   on the **absolute** `planter/cmd` and `planter/estop`, and telemetry publishes
   to the absolute `planter/telemetry`. **With a second node, both nodes would
   subscribe to `planter/cmd` and BOTH would water.** This is a safety-relevant
   bug. Fix before adding a second planter: change to per-node topics
   (`planter/<node>/cmd`, `.../estop`, `.../telemetry`) and teach `app.py`
   (§ TOPIC_* at ~line 81) to subscribe/publish per node.

3. **No pump panel in Grafana.** `grep -c pump grafana/dashboards/planter.json`
   → **0**. The "Knoten" panel is ONLINE/OFFLINE only, based on `moisture_pct`
   freshness (<60 s). The telegraf fix makes `pump` queryable *in Influx*, not
   visible on the dashboard. Add a panel if you want to see it.

4. **Telegram cooldown 600 s** — second alert within 10 min is suppressed.

5. **`fake_node.sh` defaults to `localhost:1883`**, which resolves inside the
   devbox docker network context. It printed the devbox IP as broker when run
   there. It is a devbox-local test tool; do not expect it to run from the PC.

6. **No git repo.** There is no version control here. If you make config edits,
   say so explicitly in your summary — there is no `git diff` to fall back on.
   Consider `git init` and a first commit **after** `secrets.yaml` is excluded.

---

## 11. Reference — authoritative pin map

From `esphome/smartplanter.yaml` substitutions (NOT the README):

| Function | GPIO | Note |
|---|---|---|
| Soil moisture ADC | **34** | ADC1 only. ADC2 dead while Wi-Fi on |
| Soil probe power | 25 | powered only while sampling (anti-corrosion) |
| DHT22 data | 27 | 10 kΩ pull-up to 3.3 V |
| BH1750 SDA / SCL | 21 / 22 | I²C |
| JSN-SR04T trig | **16** | |
| JSN-SR04T echo | **17** | 5 V → level shifter → 3.3 V |
| Relay IN1 (pump) | 26 | 10 kΩ pull-down to GND |
| Relay IN2 (light) | 13 | |
| Buzzer | 14 | 5 V active module |
| Button "water now" | 4 | to GND, internal pull-up |

## 12. Reference — MQTT topics

| Topic | Direction | Payload |
|---|---|---|
| `planter/telemetry` | node → hub | JSON, every 10 s |
| `planter/event` | node → hub | `{"event":"button_water_now"}` |
| `planter/status` | node → hub | `online` / `offline` (birth/will) |
| `planter/cmd` | hub → node | `{"action":"pump\|light\|buzzer\|stop", ...}` |
| `planter/estop` | hub → node | `{"estop":true\|false}` retained |
| `planter/state/*` | hub → HA | retained per-field state |

Telemetry JSON keys:
`device, fw, ts, moisture_pct, temp_c, humidity, lux, tank_pct, rssi, pump,
light, mode, on_s, fault`

API env overrides (`api/app.py` ~line 81): `TOPIC_TELEMETRY`, `TOPIC_CMD`,
`TOPIC_ESTOP`.

---

## 13. Suggested order of work

1. §3.1 secrets, §3.2 broker IP, §3.3 README pin fix — **no hardware**
2. §4.1 compile — **no hardware**, catches errors early
3. §4.2 USB flash — human plugs in
4. §4.3 bench check, §5 sensors one at a time
5. §6 relay + pump — human, bucket of water
6. §7 calibration, §8 full loop
7. §9 acceptance table — report real output for every row

Report at each phase boundary. If a check fails, stop and report rather than
working around it.
