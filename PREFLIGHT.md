# PREFLIGHT — bench checklist

Do these **in order**, on the bench, before the plant goes anywhere near a pot.
Each one is a thing that has killed a build like this at the wrong moment.

---

## 1. Power — the two-bus rule (do this before powering anything)

| Bus | Supplies | Current | Notes |
|---|---|---|---|
| **5 V logic** | ESP32 + relay coils + servo | ~1.2 A | One LM2596 **is enough here** |
| **5 V pump** | pump only, **separate buck** | ≥2 A | Inrush on start is the killer |
| **12 V** | grow light only | – | **Never** to the pump, never on a breadboard |

**The pump is 3–6 V DC. Do not switch 12 V into it.**

- [ ] Buck #1 → ESP32 5V pin. Confirm 5.0 V ±0.2 V **under load** (relay energised).
- [ ] Buck #2 (≥2 A) → pump. Pump V- and the logic GND are tied **at the buck**, one point.
- [ ] 12 V rail: **2 A inline fuse** on the way in.
- [ ] Relay coil is **not** driven straight off a GPIO — use the transistor/MOSFET board.
- [ ] Every 12 V and pump joint is **screw terminal or soldered**. Breadboard jumpers vibrate loose.

**Test:** brown out the logic side on purpose (pull the buck's input for a second). The ESP32
must come back and the pump must stay off. If the pump kicks on, fix the pull-down first.

---

## 2. Relay module — active LOW or HIGH?

- [ ] Relay module powered, ESP32 **not** yet driving the pin.
- [ ] Multimeter on the relay's IN pin:
  - reads **3.3 V** at rest → **active LOW** → set `relay_inverted: "true"`
  - reads **0 V** at rest → active HIGH → leave `"false"`
- [ ] 10 kΩ from the pump's IN pin to **GND**.
- [ ] Then: power the ESP32 with the pump connected. **The pump must not twitch.**

---

## 3. Sensors — raw values first, percentages later

- [ ] **Soil probe on GPIO 34** (ADC1). ADC2 is dead while Wi-Fi is on. Don't move it.
- [ ] Probe powered from GPIO 25, only while sampling (already in the firmware).
- [ ] DHT22: 4.7–10 kΩ pull-up between data and 3.3 V **if your module lacks one**.
  Symptom of a missing pull-up: readings of `nan` or 0, every time.
- [ ] **I²C scan** — confirm BH1750 and that its address matches the config (0x23):
  ```bash
  # while the node is up, check the ESPHome log for the scan result
  esphome logs smartplanter.yaml
  ```
  A device on 0x5C means "everything is fine" — only trust a real scan.
- [ ] JSN-SR04T echo → **level shifter** → GPIO 17. The echo line is 5 V; the ESP32 is not 5 V tolerant.

### Calibration — record these numbers, you cannot do it later

| Field | Probe in AIR | Probe in WATER | Set in `smartplanter.yaml` |
|---|---|---|---|
| Soil | `_______ V` → | `_______ V` → | `soil_dry_v` / `soil_wet_v` |

| Field | Tank FULL | Tank EMPTY | Set in `smartplanter.yaml` |
|---|---|---|---|
| Tank | `_______ cm` | `_______ cm` → | `tank_full_cm` / `tank_empty_cm` |

- [ ] Use **tap water**, not distilled (distilled reads oddly on capacitive probes).
- [ ] **Measure your tank depth first.** If it is shallower than ~25 cm the JSN-SR04T is
      blind and you should either raise the sensor or drop the tank feature entirely.

---

## 4. Pump — in a bucket, not in the pot

- [ ] Pump fully submerged before you power it. Submersible pumps **airlock** and then
      just hum. If it hums and does not move water, it needs pre-priming / a lower position.
- [ ] Time the flow rate: `_______ mL in 20 s` → decide the real `pump_seconds`.
- [ ] Run it for **60 s by hand** and feel the tubing, the relay, and the buck. Anything
      warm is a problem.
- [ ] Tubing is looped **above the tank's waterline** so it cannot siphon back after the pump stops.

---

## 5. The control test (the one the judges will actually see)

- [ ] Hold the **"Wasser jetzt"** button down for **60 s**.
      The pump must stop on its own at **45 s** (`pump_max_seconds`). This is the firmware
      ceiling, not the Pi's — the Pi is not even in the path.
- [ ] Pull the ESP32's power mid-dose. Pump off. Power back → pump still off.
- [ ] Trigger NOT-AUS from the dashboard. Pump and light off, and auto watering must
      **not** fire even at 5 % moisture.
- [ ] Clear NOT-AUS. Watering works again.

---

## 6. Network + software

- [ ] Pi has a **fixed IP** (static or DHCP reservation, not `.local` mDNS).
- [ ] `mqtt_broker` in `smartplanter.yaml` points at the **Pi**, not the devbox.
- [ ] OTA works — flash once more over Wi-Fi, no USB:
      ```bash
      esphome upload smartplanter.yaml --device <node-ip>
      ```
- [ ] Reboot the Pi. The whole stack comes back by itself (`restart: unless-stopped`).
- [ ] Reboot the Pi mid-demo-conditions: node keeps reading, pump logic resumes, chart
      survives. The sensor data path never depends on the Pi being up.

---

## 7. Mechanical — does it survive the road?

- [ ] Plant + pot strapped down. A tipping pot takes the probe and the tube with it.
- [ ] **Tank on a towel, and never above the electronics** — or spill everything.
- [ ] Probe fixed at a **consistent depth** and can't be knocked askew.
- [ ] The whole rig sits on a **closed-cell foam or anti-slip mat**, not directly on a
      hollow table. A submersible pump vibrating a table sounds like a bass drum in a
      quiet venue — a soundboard turns that into "the thing rattles".
- [ ] ESPHome's built-in **`status_led`** (or an external LED) fitted, so "is it booting?"
      is answered by looking, not by opening a laptop.
- [ ] Everything on one power strip with a **switch**, so you can kill the rig instantly.

---

## 8. Deploy to the Pi

```bash
# on the devbox — AP image last so the Pi needs no internet to build
docker save planter-api | gzip > /tmp/planter-api.tar.gz
rsync -av --exclude data --exclude 'esphome/.esphome' --exclude 'esphome/secrets.yaml' \
      /opt/stacks/smartplanter/ pi@<pi-ip>:/home/pi/smartplanter/
rsync -av /tmp/planter-api.tar.gz pi@<pi-ip>:/tmp/

# on the Pi
cd ~/smartplanter
cp .env.example .env && nano .env      # real passwords, PUID/PGID=1000
gunzip -c /tmp/planter-api.tar.gz | docker load    # no registry needed
docker compose up -d
./scripts/seed_demo_data.sh            # chart is never empty on stage
```

- [ ] `curl http://localhost:8098/health` → `"ok": true`
- [ ] `./scripts/fake_node.sh` → watch the dashboard tick over
