# Battery power (18650) — what's realistic and how to do it

**Short answer:** battery power is realistic for **sensor-only nodes** (soil /
temperature / light) — but **not** for a pump node. A 12 V diaphragm pump plus
an always-on relay is a permanent, heavy load; that stays on mains.

The trick is **deep sleep**: Wi-Fi is up for only a few seconds per reading.

---

## 1. The numbers

| Mode | Typical current | On a 2500 mAh 18650 |
|---|---|---|
| Wi-Fi TX (sending) | 80–180 mA | – |
| Wi-Fi idle / connected | ~40–60 mA | **~1–2 days** |
| Deep sleep (module) | ~10–150 µA | **months** |

Our stock firmware keeps Wi-Fi connected 24/7 to publish every 10 s — great on
USB, terrible on a cell. The `battery-template.yaml` wakes on a timer, connects,
reads, publishes, and sleeps again:

```
wake (timer) → Wi-Fi connect → read soil/temp/light/battery → publish → sleep
```

With `sleep_duration: 15min` and ~6 s awake per cycle, average draw is well under
1 mA → **a 2500 mAh cell lasts on the order of 3–6 months** (dominated by the
board's sleep leakage — see the traps below).

---

## 2. Hardware

```
 18650 (with protection) ──┬── low-Iq 3.3 V LDO ──┬── 3V3  (ESP32 + sensors)
                           │   (MCP1700/HT7333)    └── GND
                           └── divider 100k/100k ───── GPIO32 (ADC1)  battery sense
```

Rules:

- **Use a low-quiescent-current regulator** (MCP1700 ≈ 1.6 µA, HT7333 ≈ 4 µA).
  An AMS1117 (~5 mA idle!) will flatten the cell in weeks by itself.
- **Desolder the board's power LED** and, if possible, avoid a board whose
  USB-serial chip stays powered. A dev board can leak 1–7 mA in sleep, which
  dominates everything else.
- **Battery sense** through a divider (100k/100k → half the voltage; ADC1 pin).
  The template scales it back up and maps 3.30–4.20 V to 0–100 %.
- **Never** power the pump from the cell. Keep the two concerns separate.

---

## 3. Create and flash a battery node

```bash
deploy/add-node.sh new battery-a "Balcony" battery
# edit esphome/battery-a.yaml: sleep_duration, battery divider, pins
deploy/add-node.sh compile battery-a
deploy/add-node.sh flash  battery-a /dev/ttyUSB0
```

The node reports `battery_v` and `battery_pct` in its telemetry; the dashboard
and Home Assistant show them per node.

---

## 4. Tuning battery life

| Knob | Effect |
|---|---|
| `sleep_duration` (15min → 1h) | linear: longer sleep, longer life |
| `power_save_mode: LIGHT` | already set; `HIGH` saves a bit more, slower |
| remove power LED / pick a low-leak board | often the single biggest win |
| disable serial logger | tiny |
| read fewer sensors per wake | small (sensors are fast) |

Rule of thumb: **sleep leakage sets the floor**, then each extra reading costs a
fixed ~6 s of Wi-Fi. For a plant, 15–60 min resolution is plenty.

---

## 5. What does *not* work on battery

- **The pump.** 12 V, high inrush, and it must always be able to water — plus
  the relay coil draws continuously. Mains only.
- **Always-on Wi-Fi + 10 s telemetry.** That is the ~1–2 day case.
- **A board with a 5 mA idle regulator.** Fix the hardware first.

---

## 6. Traps

1. A "2500 mAh" cell is often 1200–2000 mAh; measure your runtime, don't trust
   the label. Protected cells are longer — check your holder.
2. Sleeping too aggressively makes the node look **offline** between wakes; the
   Pi's `node_silent` alert (default 5 min) will fire. Raise `alert_silent_min`
   or exclude battery nodes.
3. `deep_sleep` on the classic ESP32 needs the board to let the RTC keep time;
   most dev boards do, but a board that resets peripherals may reboot instead of
   sleeping — verify `on_s` resets are expected, not spurious.
4. Reflashing over OTA while a node is asleep can miss; flash on USB, or press
   reset with the flash ready.
