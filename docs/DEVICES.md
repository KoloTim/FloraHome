# Devices, flashing & the hotspot

FloraHome is meant to be usable **from the Pi itself**, not only from a shell.
This page covers adding a new node, flashing it, and managing the access point
the nodes join.

## Adding a plant node

```bash
deploy/add-node.sh new plant-d "My Monstera"   # esphome/plant-d.yaml
deploy/add-node.sh discover                    # which ESP is on which port
deploy/add-node.sh compile plant-d
deploy/add-node.sh flash  plant-d /dev/ttyUSB0 # first time: USB
deploy/add-node.sh ota    plant-d 10.42.0.x     # ever after: over the air
```

Node configs live in `esphome/` (next to `secrets.yaml`, so `!secret` resolves).
The node id (`device_name`) **is** its MQTT namespace: `planter/<node>/…`.

## Flash from the dashboard

The `flasher` compose service (profile `tools`) runs on the Pi's host network as
root, so it can read `/dev/ttyUSB*`. The API proxies to it:

```
GET  /api/devices           attached + known ESPs
GET  /api/nodes/available   node configs that can be flashed
POST /api/flash             { "node": "plant-d", "port": "/dev/ttyUSB0" }
```

The dashboard's **Geräte** section lists attached ESPs, shows their MAC, and can
flash a node with one tap. Each flash is written to the audit log
(`flash_requested` / `flash_done`).

> **First flash is always USB.** After a node has joined `FloraHome` once, use
> OTA (dashboard, ESPHome at `:6052`, or `deploy/add-node.sh ota`).

### Status

The flasher's **discovery** (`/devices`, `/nodes`, `/health`) is implemented. The
current iteration runs flashing through the ESPHome container against the host's
`/dev`; the Phase F TODO adds robust USB-passthrough discovery (MAC + chip via
`esptool flash_id`) and a progress stream. Until then `deploy/add-node.sh` and
ESPHome `:6052` are the reliable paths.

## The hotspot

The Pi is the access point the nodes join:

| | |
|---|---|
| SSID | `FloraHome` (2.4 GHz, band `bg`, channel 6) |
| Pi address | `10.42.0.1/24` on `wlan0` |
| Nodes | `10.42.0.x` via DHCP |
| Security | WPA2-PSK |

Because the Pi is `10.42.0.1` and owns the broker, the nodes are on the same L2
as MQTT, and **OTA works** (port 3232).

### Changing it (on the host)

```bash
sudo nmcli con mod Hotspot 802-11-wireless.ssid  "FloraHome"
sudo nmcli con mod Hotspot 802-11-wireless-security.psk "new-password"
sudo nmcli con down Hotspot && sudo nmcli con up Hotspot
# keep esphome/secrets.yaml in sync, then reflash/OTA each node once
```

The dashboard exposes `GET/PUT /api/hotspot`; the PUT is **spooled** (the API
container has no host network namespace), so apply it on the host with the
commands above. Wiring the API to the host (a small privileged helper) is on the
Phase F list.

## Device inventory

`GET /api/devices` merges what is attached right now with a small SQLite
`devices` table (name, chip, MAC, IP, port, notes). Use it to keep a tidy
inventory of your ESPs, including spares not yet deployed.
