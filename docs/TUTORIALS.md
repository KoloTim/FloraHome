# Tutorials

Hands-on walkthroughs for every part of FloraHome, from first boot to a second
plant and Home Assistant. Each tutorial ends with a **check**.

- [0 · First boot (on the Pi)](#0--first-boot-on-the-pi)
- [1 · Read the dashboard](#1--read-the-dashboard)
- [2 · Water a plant manually](#2--water-a-plant-manually)
- [3 · Calibrate the soil probe](#3--calibrate-the-soil-probe)
- [4 · Give a plant a profile (moodboard)](#4--give-a-plant-a-profile-moodboard)
- [5 · Add a second plant node](#5--add-a-second-plant-node)
- [6 · Home Assistant](#6--home-assistant)
- [7 · Alerts & Telegram](#7--alerts--telegram)
- [8 · Flash from the dashboard](#8--flash-from-the-dashboard)

---

## 0 · First boot (on the Pi)

```bash
cd ~/smartplanter
cp .env.example .env && nano .env        # set CHANGE_ME values, PUID/PGID=1000
bash scripts/start.sh                     # builds + starts + smoke tests
sudo chown -R 1000:1000 data             # once: Docker made data/ root-owned
docker compose up -d --force-recreate api
```

**Check:** `curl -s localhost:8097/health` → `"ok": true`,
`curl -s -o /dev/null -w '%{http_code}' localhost:8098` → `200`.

---

## 1 · Read the dashboard

Open `http://192.168.91.68:8098` (or look at the Pi's 5" panel).

- The **header** shows whether the live stream is up and how many nodes are online.
- **Meldungen** lists active alerts.
- **Pflanzen** shows one card per plant: mood, big soil-moisture %, temperature,
  air humidity, light, and the pump state.

**Check:** the card's green dot is lit and `online · …` ticks down.

---

## 2 · Water a plant manually

1. Tap **Anmelden** and log in (`ADMIN_USER` / `ADMIN_PASSWORD` from `.env`).
2. Tap **💧 Bewässern**. The node runs a one-shot dose (default 20 s).

The firmware **clamps** any dose to `pump_max_seconds` (45 s) and a running dose
cannot be extended. The same action exists as the physical "Wasser jetzt" button
and as a Home Assistant button.

**Check:** the card's pump tag goes *Bewässert 💧* → then *Sperrzeit ⏳* → *Bereit*.

---

## 3 · Calibrate the soil probe

`moisture_pct` is meaningless until two voltages are measured.

1. Watch the **Boden Rohspannung** in the card (or in the calibration section).
2. Probe in **air** → type that voltage into *In Luft (trocken)*.
3. Probe in a **glass of tap water** → type that into *In Wasser (nass)*.
4. Tap **Werte übernehmen**.

This is sent to the node as `{"action":"cal", …}` and **persisted on the node**,
so it survives reboots with no reflash.

**Check:** air reads ~0–10 %, water ~90–100 %.

---

## 4 · Give a plant a profile (moodboard)

1. Tap **Profil bearbeiten**.
2. Pick a **species** (e.g. *Fensterblatt / Monstera*) — the card shows the care
   range (moisture, temperature, light, watering interval, difficulty).
3. Set a **nickname**, **emoji** and **color**.
4. Save.

The card now shows the plant's name, species, and a live **mood** — *glücklich*,
*durstig*, *zu nass*, *schläft* (offline), *braucht Hilfe* (fault) — computed from
the reading vs the species' care range.

**Check:** the mood text changes when you move the probe between air and water.

---

## 5 · Add a second plant node

```bash
deploy/add-node.sh new plant-b "My Snake Plant"
deploy/add-node.sh discover          # find the port for the new ESP
deploy/add-node.sh compile plant-b
deploy/add-node.sh flash  plant-b /dev/ttyUSB0
```

After it joins `FloraHome`, update it over the air:

```bash
deploy/add-node.sh ota plant-b 10.42.0.x      # or the ESPHome UI at :6052
```

Every node has its **own** MQTT namespace (`planter/plant-b/…`), so a dose to one
plant can never water another. Repeat tutorial 4 to profile it.

**Check:** `curl -s localhost:8097/api/state` lists `plant-a` **and** `plant-b`.

---

## 6 · Home Assistant

See [HOMEASSISTANT.md](HOMEASSISTANT.md). In short: add the MQTT integration
pointing at `192.168.91.68:1883` (user `planter`). Each plant appears as its own
device with sensors (moisture, temperature, humidity, light, RSSI, pump) plus
**Jetzt bewässern**, **Buzzer testen** and a **Grow Light** switch.

**Check:** the device shows live values and the water button triggers a dose.

---

## 7 · Alerts & Telegram

1. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`, restart the stack.
2. To test `node_silent`: unplug a node's power; after `alert_silent_min`
   (default 5 min) you get a Telegram message and a Meldung appears.

Rule table, cooldowns and tuning live in [ALERTS.md](ALERTS.md).

**Check:** a Telegram message arrives; the alert clears once the node is back.

---

## 8 · Flash from the dashboard

1. Plug the ESP into the Pi by **USB data cable** (not charge-only).
2. `docker compose --profile tools up -d flasher` (and `esphome` for the UI).
3. In the dashboard, the **Geräte** section lists attached ESPs; pick a node and
   tap **Flashen**.

Details, limits and the host-side commands: [DEVICES.md](DEVICES.md).

**Check:** `GET /api/devices` lists your port; the audit log shows `flash_done`.
