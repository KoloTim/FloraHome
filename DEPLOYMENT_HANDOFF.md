# Smart Planter — Deployment Handoff (Pi + ESP32)

Status as of **2026-09-28**. This documents what was actually deployed, how to
reproduce it, how to operate it, and every trap we hit.

The original `HANDOFF_OPENCODE.md` describes the *intended* bring-up from the
devbox. This file describes the **real** deployment on the demo Raspberry Pi and
the real ESP32, including deviations.

---

## 1. What is running

| Piece | Identity |
|---|---|
| **Pi (demo host)** | hostname `Tim`, `192.168.91.68`, Raspberry Pi 4 Model B Rev 1.2, Raspberry Pi OS 13 (trixie) aarch64, user `tim` |
| **Devbox (source + builder)** | `kolotim@192.168.178.185`, Docker 29.8.1, stack at `/opt/stacks/smartplanter` |
| **ESP32 node** | ESP32-D0WD-V3 (Wemos D1 mini ESP32), MAC `5c:01:3b:be:98:f4`, CP2102 USB-UART |
| **Stack** | Docker Compose project `smartplanter` → `planter-mosquitto`, `planter-influxdb`, `planter-telegraf`, `planter-api`, `planter-web`, `planter-grafana` |

### URLs (from the LAN)

| Service | URL |
|---|---|
| Dashboard | http://192.168.91.68:8098 |
| API docs / state | http://192.168.91.68:8097/docs · `/api/state` |
| Grafana | http://192.168.91.68:3030 (anonymous = Viewer) |
| InfluxDB | http://192.168.91.68:8086 |
| MQTT | `tcp://192.168.91.68:1883`, user `planter` |

### Data path

```
ESP32 ──MQTT──▶ mosquitto ──▶ telegraf ──▶ influxdb ──▶ grafana
                            └▶ api ──▶ web dashboard (SSE) / Telegram / audit
```

The ESP32 holds all pump safety (relay default-off, 45 s firmware ceiling,
`mode: single` dose). The Pi only decides *when*. If the Pi dies mid-watering the
relay still opens.

---

## 2. Network topology (important)

- The Pi is on the wired LAN `192.168.91.0/24` (`eth0 = 192.168.91.68`).
- The Pi's `wlan0` is on `FortLife` (5 GHz, `10.34.53.x`) — not used by the node.
- The ESP32 joins **`Group1`** (2.4 GHz). `Group1` is a **NATed hotspot**:
  its WAN address on the LAN is `192.168.91.39` (BSSID `D8:0D:17:83:D3:1F`).
- Consequence: the node can reach the broker **outbound**, but the node's own
  **web UI (`:80`) and ESPHome OTA (`:3232`) are NOT reachable** inbound from the
  Pi/PC. Telemetry, auto-water commands and the retained e-stop all work because
  the node subscribes outbound.
- **To restore web UI + OTA**, move the node to a flat 2.4 GHz network that
  bridges onto `192.168.91.0/24` (or set up the Pi as an AP), update
  `esphome/secrets.yaml` and reflash.

---

## 3. Reproduce the Pi deployment

Prereqs: Pi reachable over SSH (`tim@192.168.91.68`), sudo password, internet.

```bash
# 1. Docker Engine + compose (official script)
curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
sudo sh /tmp/get-docker.sh
sudo usermod -aG docker tim          # re-login for the group to apply

# 2. Ship the tree from the devbox (rsync over your PC if the two are not
#    mutually routable). The archive excludes build cache, data and secrets:
tar czf /tmp/smartplanter-src.tgz \
  --exclude='esphome/.esphome' --exclude='data' \
  --exclude='.env' --exclude='esphome/secrets.yaml' \
  -C /opt/stacks smartplanter
#   ...copy it to the Pi, then:
mkdir -p ~/smartplanter && tar xzf ~/smartplanter-src.tgz -C ~

# 3. Create .env from the devbox's working .env (PUID/PGID=1000) and place it
mv ~/smartplanter.env ~/smartplanter/.env

# 4. First start
cd ~/smartplanter && bash scripts/start.sh
```

### The one mandatory fix after first start

Docker creates the `data/` bind mount as **root**, but the API runs as
`1000:1000` → `sqlite3.OperationalError: unable to open database file` and the
container restart-loops. Fix once:

```bash
cd ~/smartplanter
sudo chown -R 1000:1000 data
docker compose up -d --force-recreate api
```

Then seed history so the demo chart is never empty:

```bash
./scripts/seed_demo_data.sh
```

---

## 4. Build + flash the ESP32 firmware

The firmware is the stock `esphome/smartplanter.yaml` with **one substitution
changed**: `mqtt_broker: "192.168.91.68"` (the Pi).

### Build (do it off the Pi — the 15 GB SD cannot hold the ~3 GB toolchain)

The devbox already has the PlatformIO cache, so build there (≈90 s):

```bash
# on the devbox, with a temporary copy of the config:
cd /opt/stacks/smartplanter
cp -a esphome/smartplanter.yaml /tmp/bak.yaml
sed -i 's|mqtt_broker: "192.168.178.185"|mqtt_broker: "192.168.91.68"|' esphome/smartplanter.yaml
# write esphome/secrets.yaml from .env (wifi Group1, mqtt creds from .env)
bash scripts/build_firmware.sh
cp esphome/.esphome/build/smartplanter/.pioenvs/smartplanter/firmware.factory.bin /tmp/
cp -a /tmp/bak.yaml esphome/smartplanter.yaml       # restore
```

`firmware.factory.bin` is a **merged** image → flash it at **offset 0x0**.

### Flash (first flash is always USB)

```bash
# on the Pi; node on /dev/ttyUSB0, tim is in the dialout group
sudo apt-get install -y esptool
esptool --chip esp32 --port /dev/ttyUSB0 --baud 460800 \
  --before default_reset --after hard_reset \
  write_flash -z --flash_mode dio --flash_freq 40m --flash_size detect \
  0x0 ~/smartplanter-factory.bin
```

**Gotcha:** Debian's esptool 4.7 package is missing
`targets/stub_flasher/stub_flasher_32.json`, so the normal (stub) path crashes.
Add **`--no-stub`** — it flashes fine.

### Secrets

`esphome/secrets.yaml` (gitignored) must contain:

```yaml
wifi_ssid: "Group1"
wifi_password: "…"
ap_password: "…"
ota_password: "…"
mqtt_username: "planter"
mqtt_password: "…"        # == MQTT_PASSWORD in .env
```

The compiled `firmware.factory.bin` **contains the Wi-Fi and MQTT passwords** —
never commit or publish it.

---

## 5. Build firmware on the PC instead (no devbox)

```bash
python -m pip install --user esphome==2025.8
cd esphome && cp secrets.yaml.example secrets.yaml   # fill in
esphome compile smartplanter.yaml
# artifacts: esphome/.esphome/build/smartplanter/.pioenvs/smartplanter/
```

Toolchain download ≈ 1.5 GB. Needs ~3 GB free.

---

## 6. SD-card clone (15 GB → 32 GB)

`rpi-clone` runs **on the Pi** against a card in a USB reader.

```bash
# install (deps: rsync parted dosfstools)
git clone --depth 1 https://github.com/billw2/rpi-clone.git
sudo cp rpi-clone/rpi-clone /usr/local/sbin/ && sudo chmod +x /usr/local/sbin/rpi-clone

# stop heavy writers for a consistent snapshot
cd ~/smartplanter && docker compose stop
sudo umount /dev/sda                       # reader target, if auto-mounted
sudo rpi-clone sda -f -U                   # -f force-init, -U unattended
```

`rpi-clone` images the booted partition layout, syncs the filesystems and grows
the destination root to fill the card. When done: power off, swap cards, boot.

**Power note:** plugging the USB reader caused one Pi reboot during the first
attempt. Use a good supply; keep hot-plugging to a minimum.

---

## 7. Operations cheat-sheet

```bash
cd ~/smartplanter
docker compose ps                 # status
docker compose logs -f api        # api logs
docker compose restart telegraf   # if "no such host mosquitto" on cold boot
./scripts/fake_node.sh --wet      # publish one healthy fake reading
./scripts/seed_demo_data.sh       # 24 h of demo history
./scripts/test_estop.sh           # end-to-end NOT-AUS test (expect ✅ PASS)
curl -s localhost:8097/api/state | python3 -m json.tool
```

### Watch live telemetry (password read from `.env`, never printed)

```bash
cd ~/smartplanter && set -a && . ./.env && set +a
docker run --rm --network smartplanter_default eclipse-mosquitto:2 \
  mosquitto_sub -h mosquitto -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
  -t 'planter/telemetry' -v
```

---

## 8. Acceptance status

| # | Check | Result |
|---|---|---|
| 1 | Node web UI `http://<node-ip>/` → 200 | ❌ **blocked by Group1 NAT** (not a firmware fault) |
| 2 | Telemetry every 10 s on `planter/telemetry` | ✅ real node, every 10 s |
| 3 | Node registered, `online:true`, age < 30 s | ✅ |
| 4 | `fault` reported | ✅ (`dht22,bh1750,jsn_sr04t,` — sensors not yet wired) |
| 5 | `pump` reaches InfluxDB (string field) | ✅ verified with fake data |
| 6 | E-stop suppresses watering | ✅ `test_estop.sh` PASS |
| 7 | Audit trail written | ✅ |

---

## 9. Known issues / traps

1. **`data/` owned by root on first boot** → API restart-loop. `chown 1000:1000 data`.
2. **esptool package broken stub** → always add `--no-stub` for ESP32.
3. **Disk:** 15 GB SD is tight; the ESPHome toolchain overflowed it (89% full).
   Keep builds off the Pi, or use a bigger card.
4. **`Group1` is NATed** → node web UI/OTA unreachable inbound (see §2).
5. **Firmware `fault` trailing comma** — `"dht22,bh1750,jsn_sr04t,"`. Cosmetic
   (the space→comma loop in the telemetry lambda leaves one). API parses it.
   Fix in `esphome/smartplanter.yaml` telemetry lambda if you rebuild.
6. **Wrong board first time:** an ESP8266 (Wemos D1 mini) was initially plugged
   in; the ESP32 image cannot flash to it. Always confirm with `esptool flash_id`.
7. **Per-node MQTT topics not implemented** (known from the original handoff):
   a second node would share `planter/cmd`. Fix before adding a second planter.
8. **`fake_node.sh` uses device `planetest`** and shares ONE global API state
   with the real node. Do not run it while the real node is live.

---

## 10. Secrets policy

Committed: `.env.example`, `esphome/secrets.yaml.example`.
**Never committed:** `.env`, `esphome/secrets.yaml`, `data/`, `esphome/.esphome/`,
compiled firmware (contains Wi-Fi/MQTT passwords).

To read a secret for tooling, source `.env` on the host — do not paste it into
chat or commit it.
