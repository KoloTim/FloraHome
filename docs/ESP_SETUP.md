# ESP32 setup & flashing

Everything needed to identify, build for, flash and verify the ESP32 node(s).
For wiring see [`ACTUAL_HARDWARE.md`](ACTUAL_HARDWARE.md).

## Boards

| Name | Chip | MAC | Where |
|---|---|---|---|
| Node #1 (plant A) | ESP32-D0WD-V3 | `5c:01:3b:be:98:f4` | /dev/ttyUSB0 on the Pi |
| Node #2 (plant B) | ESP32-D0WD-V3 | `e0:8c:fe:e5:82:f4` | /dev/ttyUSB1 on the Pi |
| Spares | ESP8266 D1 mini, ESP32-CAM | – | not used |

Identify whatever is plugged in (never flash blind):

```bash
ls -l /dev/ttyUSB*
for d in /dev/ttyUSB*; do echo "== $d =="; esptool --port "$d" flash_id 2>&1 | grep -iE 'Chip is|MAC:'; done
```

> Two identical ESP32s are attached. **Only Node #1 runs the firmware today.**
> Do **not** flash the same config to Node #2 — the project uses absolute MQTT
> topics, so a second node would also answer `planter/cmd` and *both would
> water*. Multi-node is a code change, tracked in `BRINGUP_PLAN.md` Phase F.

## Secrets

`esphome/secrets.yaml` (gitignored) must contain:

```yaml
wifi_ssid: "Group1"
wifi_password: "password"
ap_password: "planter-fallback"
ota_password: "planter-ota"
mqtt_username: "planter"
mqtt_password: "<MQTT_PASSWORD from .env>"
```

## Build (off the Pi — its SD is too small for the toolchain)

Use the devbox (PlatformIO cache present → ~2 min). On the build host:

```bash
cd /opt/stacks/smartplanter
cp -a esphome/smartplanter.yaml /tmp/yaml.bak
cp -a esphome/secrets.yaml   /tmp/secrets.bak
# put the repo's esphome/smartplanter.yaml in place, write secrets.yaml (above)
bash scripts/build_firmware.sh
cp esphome/.esphome/build/smartplanter/.pioenvs/smartplanter/firmware.factory.bin /tmp/
cp -a /tmp/yaml.bak esphome/smartplanter.yaml
cp -a /tmp/secrets.bak esphome/secrets.yaml
```

`firmware.factory.bin` is a **merged** image → flash at **0x0**.
(The OTA image is the separate `firmware.ota.bin`.)

## Flash over USB (first flash / after a Wi-Fi change)

On the Pi, with the target on `/dev/ttyUSB0`:

```bash
esptool --chip esp32 --no-stub --port /dev/ttyUSB0 --baud 460800 \
  --before default_reset --after hard_reset \
  write_flash -z --flash_mode dio --flash_freq 40m --flash_size detect \
  0x0 ~/smartplanter-factory.bin
```

**`--no-stub` is required** on the Pi — Debian's esptool 4.7 package ships
without `targets/stub_flasher/stub_flasher_32.json`, so the normal path crashes.

## Network reality (read this)

`Group1` is a **NATed hotspot**. The MQTT broker sees the node as coming from
`192.168.91.39` (the hotspot's WAN address, not the ESP32). Consequence:

- ✅ telemetry, commands and the retained NOT-AUS all work (node connects out).
- ❌ the node's **web UI (`:80`) and OTA (`:3232`)** are **not reachable**.

To enable OTA you must move the node to a **flat 2.4 GHz network that bridges
onto `192.168.91.0/24`** (then update `secrets.yaml`, reflash once, and OTA
works), or make the Pi an access point. Until then, every firmware change is a
**USB** flash.

## Verify after flashing

```bash
# serial (resets the chip and prints the boot log)
python3 ~/node_log.py 25

# broker view
cd ~/smartplanter && set -a && . ./.env && set +a
docker run --rm --network smartplanter_default eclipse-mosquitto:2 \
  mosquitto_sub -h mosquitto -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t 'planter/telemetry' -v

# API
curl -s http://localhost:8097/api/state | python3 -m json.tool
```

Expect (firmware **1.1.0**, with sensors wired): `moisture_pct`, `temp_c`,
`humidity`, `lux`, `fault: "ok"`. Unwired sensors appear by name in `fault`.

## Sensor sanity (Phase A)

| Sensor | Pass looks like |
|---|---|
| DHT11 (GPIO27) | `[I][dht:…] Temperature … Humidity …` every 30 s |
| LDR (GPIO35) | `lux` changes when you shade it |
| HW-390 (A0 GPIO34) | `Bodenfeuchte Rohspannung` changes air vs water |
