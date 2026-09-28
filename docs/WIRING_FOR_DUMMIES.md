# Wiring for dummies — breadboard, ESP32, one sensor at a time

Follow this in order. After **each** sensor, check the dashboard and only move on
when it shows a sane value. Unplug the ESP32's USB **before** you change any
wires, plug it back in to test.

Dashboard from any device on the LAN: **http://192.168.91.68:8098**
Tiles to watch: *Temperatur*, *Luftfeuchte*, *Bodenfeuchte*, *Licht*, and the
red **Fault** line (it names any sensor that failed).

---

## 0. The two things you must understand first

### The breadboard
```
   red  (long rail, we use it for 3.3V)  ─────────────────────────
   blue (long rail, we use it for GND)   ─────────────────────────

   a b c d e   │   f g h i j        <- each short group of 5 holes is
   • • • • •   │   • • • • •           connected together, row by row
   • • • • •   │   • • • • •        <- the middle trench separates the halves
   • • • • •   │   • • • • •
```
- The **two long side rails** (red + / blue −) run the whole length — use them as
  the shared **3.3 V** and **GND**.
- The **short rows** (a–e on the left, f–j on the right) connect 5 holes each.
  Put the ESP32 across the middle trench so each pin lands in its own row.

### The ESP32 pin labels
Every header pin has its number **printed on the board**. You only need six for
now:

| Printed label (either form) | What it is | Use |
|---|---|---|
| `3V3` / `3.3V` | 3.3 V power | sensor VCC (except soil!) |
| `GND` | ground | sensor GND |
| `VIN` / `5V` | 5 V **— do NOT use yet** | leave alone |
| `D27` or `27` | GPIO 27 | DHT11 data |
| `D25` or `25` | GPIO 25 | soil sensor power |
| `D34` or `34` | GPIO 34 (input only) | soil sensor signal |
| `D35` or `35` | GPIO 35 (input only) | LDR signal |
| `EN` / `BOOT` | reset / boot | leave alone |

> GPIO 34 and 35 are **input-only** (perfect for analog sensors). Never wire a
> sensor's power to them.

**Step 0 — set up the rails (ESP32 unplugged):**
1. Put the ESP32 across the centre trench, USB socket overhanging the edge.
2. Jumper **`3V3` → red rail**.
3. Jumper **`GND` → blue rail**.
4. (38-pin boards have a second `3V3`/`GND` — jumper those to the rails too.)

Now every sensor just needs: VCC→red rail, GND→blue rail, and one signal pin.

---

## Sensor 1 — DHT11 (temperature + humidity)  ← start here

Shows up as **Temperatur** and **Luftfeuchte** on the dashboard.

| DHT11 pin | Connect to |
|---|---|
| VCC (or +) | **red rail (3.3 V)** |
| GND (or −) | **blue rail (GND)** |
| DATA / SIG / OUT | **GPIO 27** |
| (4th pin) NC | leave empty |

Plus, **only if your module has no built-in resistor**:
**10 kΩ between GPIO 27 and the red rail** (one leg in the DATA row, one leg in
the red rail).

Steps:
1. USB **out**.
2. Push the DHT11 into the breadboard (its 3/4 legs land in 3/4 rows).
3. Wire VCC → red, GND → blue, DATA → the row where you plug GPIO 27.
4. Add the 10 kΩ pull-up if needed.
5. USB **in**.
6. Wait ~30–45 s. Dashboard should show ~20 °C and ~50 %. The fault line clears.

**If it still fails**, you'll see this on the node's serial log:
`[W][dht:050]: Invalid readings! Check pin number and pull-up resistor.`
→ pull-up missing, DATA on the wrong pin, or the sensor is really a DHT22
(then we rebuild the firmware with `model: DHT22`).

---

## Sensor 2 — HW-390 capacitive soil probe

Shows up as **Bodenfeuchte**. ⚠️ The power pin is different on purpose:

| HW-390 pin | Connect to |
|---|---|
| VCC | **GPIO 25** (NOT the rail — the firmware powers it only while sampling) |
| GND | **blue rail** |
| AOUT / A0 | **GPIO 34** |

Steps:
1. USB out.
2. Wire VCC → the row for **GPIO 25**, GND → blue, AOUT → row for **GPIO 34**.
3. USB in. The value updates every ~30 s.
4. It will read ~100 % until calibrated — that is normal and expected.

---

## Sensor 3 — LDR (light)

Shows up as **Licht**. This one is not a module — it is a light-dependent
resistor you make into a voltage divider:

```
   3V3 (red) ──[ LDR ]──┬──────── GPIO 35
                        └──[ 10 kΩ ]── GND (blue)
```

Steps:
1. USB out.
2. LDR leg 1 → red rail.
3. LDR leg 2 + 10 kΩ leg 1 + jumper to **GPIO 35** all in the same short row.
4. 10 kΩ leg 2 → blue rail.
5. USB in. Cover the LDR with your hand → the *Licht* number should drop.

---

## How to watch it working

- Open **http://192.168.91.68:8098** from your PC/phone. The tiles update every
  10 s over the live (SSE) feed.
- The **Fault** line names any sensor that isn't reporting — a fast checklist.

## Safety / rules

- **Unplug USB before re-wiring.** Only 3.3 V on the breadboard for now.
- The **12 V pump/light do NOT go on the breadboard** — that's the later phase
  with screw terminals and a separate supply.
- Never feed a sensor from the `VIN`/`5V` pin in this phase.
- Keep the ESP32 and jumpers clear of the 12 V wiring when we get there.

## Troubleshooting quick table

| Symptom | Cause / fix |
|---|---|
| DHT11 `Invalid readings` | missing 10 kΩ pull-up, wrong pin, or it's a DHT22 |
| Bodenfeuchte stuck at 100 % | AOUT on wrong pin, VCC not on GPIO25, or uncalibrated |
| Licht always ~0 | LDR/10 kΩ swapped, or reading is genuinely dark |
| Fault lists a sensor you wired | re-seat the jumper in the correct **GPIO** row |
| Nothing on dashboard | check the node is online: `curl http://192.168.91.68:8097/api/state` |
