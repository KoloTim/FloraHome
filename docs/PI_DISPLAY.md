# Local display on the Pi — plant overview

Goal: the Pi drives a small screen (HDMI or DSI) showing a live "greenhouse"
overview of the plant(s), without opening a laptop.

Because the whole stack already serves a self-contained dashboard on
`http://localhost:8098`, the cheapest good option is a **kiosk browser** on the
Pi. No new backend, no extra points of failure.

---

## Option A — kiosk browser (recommended)

Reuses the existing dashboard (`web/index.html` → API → SSE). The Pi OS Desktop
already includes Chromium.

### One-shot setup

```bash
# on the Pi
bash deploy/setup-kiosk.sh          # from a checkout of this repo, on the Pi
```

That script installs Chromium if missing, disables blanking, and drops an
autostart entry so the dashboard opens full-screen after login.

### What it runs

```
chromium-browser --kiosk --noerrdialogs --disable-infobars \
  --incognito --check-for-update-interval=31536000 \
  http://localhost:8098/
```

### Turn on desktop autologin

The kiosk starts on graphical login. Enable autologin once:

```bash
sudo raspi-config            # System Options -> Boot / Auto Login -> Desktop Autologin
```

### Screen tweaks

- Rotate a portrait panel: add `display_rotate=1` (or `2`/`3`) to
  `/boot/firmware/config.txt`, reboot.
- Blanking is disabled by the autostart entry (`xset s off; xset -dpms`).

---

## Option B — bespoke full-screen app

If the web dashboard is too heavy for a very small screen, render your own tiles
from `GET /api/state` (JSON) with Python + Tkinter/Pygame:

- big **Bodenfeuchte %** with a trend arrow
- **Tank %**, **Temperatur**, **Licht**
- **Pumpe** state + last watering time
- alert banner when `fault != "ok"` or an alert is active

Useful endpoint: `GET /api/state` returns `metrics`, `pump`, `fault`, `alerts`,
`online`, `last_seen_age_s`, `pump_count_today`. SSE stream at `/api/stream`
for push updates.

---

## Grafana kiosk (Option C)

Grafana is already anonymised as **Viewer** on `:3030`. Point the kiosk at a
specific dashboard (`/d/<uid>?kiosk&refresh=10s`) for a pure-graph wall display.

---

## Multi-plant note (read before adding a second ESP)

The API holds **one global STATE** and the firmware uses **absolute** MQTT topics
(`planter/cmd`, `planter/estop`, `planter/telemetry`). A second node would share
`planter/cmd` and **both would water**. Before a second planter:

1. Switch the firmware to per-node topics `planter/<node>/...`.
2. Teach `api/app.py` (`TOPIC_*`, ~line 81) to subscribe/publish per node and
   keep a dict of per-node state.
3. Give the display a per-plant layout fed by that dict.

Tracked as known issue #7 in `../DEPLOYMENT_HANDOFF.md`.
