# Calibration

`moisture_pct` and `lux` are only meaningful after calibration. Firmware **1.2.0**
publishes raw voltages and lets you set the soil calibration **at runtime over
MQTT** (persisted across reboots) — so no re-flash is needed for each tweak.
That matters here because OTA is blocked behind the `Group1` NAT.

## Live raw values

```bash
# on the Pi
bash deploy/calibrate.sh watch
```
```
soil_v=1.842   ldr_v=2.831   moisture=42   lux=8576   cal=(2.5, 1.2)
```

- `soil_v` — HW-390 raw output (V)
- `ldr_v`  — LDR divider midpoint (V)
- `cal_dry` / `cal_wet` — the two soil points currently in use

## Soil — guided 2-point calibration

```bash
bash deploy/calibrate.sh soil
```

1. Probe **in air** → Enter → it samples `soil_v` for 12 s → that's `dry`.
2. Probe in a **glass of tap water** → Enter → samples → that's `wet`.
3. It publishes `{"action":"cal","soil_dry_v":…,"soil_wet_v":…}` to
   `planter/cmd`. The node stores it and recomputes `moisture_pct`.

Manual form: `bash deploy/calibrate.sh soil-set 2.55 1.15`

Notes:
- Use **tap water**, not distilled (capacitive probes read distilled oddly).
- Capacitive (HW-390): dry is the **higher** voltage, wet the **lower** one.
- Formula: `pct = (dry - measured) / (dry - wet) * 100`.

## Light (LDR)

`lux` is an **uncalibrated relative** scale (0–10000), because an LDR is not a
real lux meter. What matters is that it **moves**:

- bright vs covered should change `ldr_v` clearly.
- If `ldr_v` is stuck high (~2.8 V) and does not move when you shade the LDR,
  the divider is mis-wired (see below), not a calibration problem.

## LDR divider wiring (re-check)

```
   3V3 (red) ──[ LDR ]──┬── GPIO 35
                        └──[ 10 kΩ ]── GND (blue)
```

Common mistakes that pin the reading high:
- **Both LDR legs in the same breadboard row** → the LDR is shorted out.
- The 10 kΩ goes to **3V3** instead of GND (then the node sees ~3.3 V always).
- The jumper to GPIO 35 is in a different row than the LDR/10 kΩ junction.
- Breadboard **power rails are split** in the middle — jumper the two halves.

Quick check: `bash deploy/calibrate.sh watch`, cover the LDR with your hand for
~30 s; `ldr_v` should drop. If it doesn't, it's the wiring above.

## Runtime command reference

| MQTT topic | Payload |
|---|---|
| `planter/cmd` | `{"action":"cal","soil_dry_v":2.55,"soil_wet_v":1.15}` |
