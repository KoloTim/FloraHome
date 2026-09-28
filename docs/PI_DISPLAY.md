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
# on the Pi, from a checkout of this repo
bash deploy/setup-kiosk.sh    # installs deploy/kiosk.sh + autostart + policy
```

That script installs Chromium if missing, drops the launcher `deploy/kiosk.sh`,
installs a Chromium policy that disables the translate bubble, and adds an
autostart entry so the dashboard opens full-screen after login.

### What it runs — `deploy/kiosk.sh`

```bash
chromium --kiosk \
  --ozone-platform=wayland --enable-features=UseOzonePlatform \
  --disable-features=Translate,TranslateUI,MediaRouter,OptimizationHints \
  --disable-translate --noerrdialogs --disable-infobars --hide-scrollbars \
  --lang=de --force-device-scale-factor=0.85 \
  --user-data-dir="$HOME/.config/planter-kiosk" \
  http://localhost:8098/
```

`--ozone-platform=wayland` is required on the Pi OS **labwc** session — without
it Chromium tries X11 and exits with *"Missing X server or $DISPLAY"*. The scale
factor makes the whole overview fit the 800×480 panel.

### Touch

The kiosk is touch-ready, but the panel's **USB touch cable must be connected**
to the Pi — a 5" HDMI panel carries video only over HDMI. Verify with `lsusb`
(a HID touchscreen should appear) and `libinput list-devices`. Add
`--touch-events=enabled` to the Chromium flags if taps are not registering, and
a libinput calibration/rotation matrix if the axes are rotated.

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
