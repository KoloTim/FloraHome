# Actual hardware — parts, wiring, firmware deltas

This supersedes the generic assumptions in `../PREFLIGHT.md` / `../README.md`
for **our** build. Firmware: `esphome/smartplanter.yaml` (v1.1.0, customised).

## Board

ESP32 WROOM **30/38-pin DevKit** (ESP32-D0WD-V3, CP2102). GPIO numbers are
printed on the header — wire to the **GPIO number**, not a D-label.
All logic is **3.3 V**. **ADC1 only** (GPIO 32/33/34/35/36/39) while Wi-Fi is on.

## Bill of materials

| Part | Notes |
|---|---|
| ESP32 WROOM DevKit | the node |
| DHT11 (v1.2 board) | temp/humidity, single-wire |
| LDR (GL55xx) + 10 kΩ | light, replaces BH1750 |
| HW-390 capacitive soil probe | analog out |
| iduino resistive probe | spare |
| HLS8L DC3V relay module | pump + light switching |
| R385 **12 V** diaphragm pump | relay-switched |
| 12 V grow light | relay-switched |
| 5" 800×480 HDMI display | Pi kiosk |
| LM2596 buck | 12 V → 5 V logic |
| 12 V PSU, breadboard, jumpers | bench power |
| NPN transistors, diodes, resistors | relay/buzzer drivers if needed |
| ESP8266 D1 mini, ESP32-CAM | spares (not used yet) |

## Sensor wiring (Phase A — low voltage, safe)

```
DHT11 (v1.2):   VCC -> 3V3      GND -> GND      SIG/DATA -> GPIO27
LDR divider:    3V3 -> LDR -> +-- GPIO35 --+ -> 10k -> GND
HW-390:         VCC -> GPIO25   GND -> GND      AOUT -> GPIO34
```

- **DHT11**: set `model: DHT11` in firmware (done). Add a 10 kΩ pull-up
  DATA↔3V3 only if the board lacks one.
- **LDR divider**: brightness ↑ → LDR resistance ↓ → GPIO35 voltage ↑.
  `lux` is an *uncalibrated relative* value (0–10000), documented as such.
- **HW-390**: powered from **GPIO25 only while sampling** (anti-corrosion, in
  firmware). Its AOUT is ratiometric to VCC; calibrate in air vs tap water.

## Actuator wiring (Phase C — only after jumpers are proven)

```
                +12V ──[2A fuse]──┬──────────── relay COM (pump)
                                   │
                           relay NO ┴── pump +      pump - ── GND(12V)
                           (diode across pump, cathode to +)

Relay module:  VCC -> 5V(buck, or 3V3 if module needs it)
               GND -> GND
               IN1 -> GPIO26 (pump)      IN2 -> GPIO13 (light)
Buzzer:        VCC/5V, GND, SIG -> GPIO14  (or NPN driver if bare)
```

Rules that still apply:

- **Logic 5 V** (ESP32 + relay coils) and **12 V** (pump + light) are separate
  rails; grounds tied at the supply, one point.
- **Never** put 12 V on the breadboard — screw terminals / soldered joints.
- **Fuse** the 12 V feed (2 A).
- Relay coil must not be driven by a GPIO directly — the module has its driver,
  otherwise use NPN + flyback diode + base resistor.
- Add the **10 kΩ pull-down on GPIO26** so the pump can't twitch at boot.
- Verify the relay's **contact rating ≥ pump inrush** (R385 runs ~0.5–0.7 A,
  but diaphragm inrush is higher). If the HLS8L contacts are marginal, switch
  the pump with a **MOSFET** instead.
- Confirm active level: with the module powered but not driven, if IN reads
  3.3 V at rest it is **active LOW** → set `relay_inverted: "true"`.

## 18650 batteries

- OK for the **5 V logic** (1S + 5 V boost, or 2S + buck).
- **Not** for the pump: 12 V motor, high inrush. Bench from the 12 V PSU.

## Firmware deltas vs the stock config

| Stock | Ours |
|---|---|
| DHT22 | **DHT11** |
| BH1750 (I²C) | **LDR on ADC GPIO35** (relative lux) |
| JSN-SR04T + `tank_pct` | **removed** (no tank sensor) |
| `fault` names | `soil,dht11,ldr` (+ trailing-comma bug fixed) |
| `fw_version` | `1.1.0` |

`tank_pct` simply won't appear in telemetry; the API/UI tolerate that.

## Build + flash (Wi-Fi is moving to the flat LAN)

1. Put the flat-LAN SSID/password in `esphome/secrets.yaml`.
2. Build off-box (devbox cache = ~90 s) → `firmware.factory.bin`.
3. **USB** flash at `0x0` (first flash after a Wi-Fi change):
   `esptool --chip esp32 --no-stub --port /dev/ttyUSB0 --baud 460800 write_flash -z 0x0 firmware.factory.bin`
4. After it joins the flat LAN, **OTA** works:
   `esphome run smartplanter.yaml --device <node-ip>`.
