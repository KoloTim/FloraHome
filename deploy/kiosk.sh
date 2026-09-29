#!/usr/bin/env bash
# FloraHome kiosk launcher: keep the dashboard on screen, full screen.
# Installed by deploy/setup-kiosk.sh and started by the labwc session autostart.
#
# Resilient by design: waits for the dashboard, then relaunches Chromium if it
# ever exits (e.g. a crash), so the panel never ends up on a blank desktop.
set -u

URL="${PLANTER_URL:-http://localhost:8098/}"
PROFILE="$HOME/.config/planter-kiosk"
LOG="$PROFILE/kiosk.log"
mkdir -p "$PROFILE"

log() { echo "$(date '+%F %T') $*" >>"$LOG"; }

# Wait up to 180 s for the dashboard (the Docker stack may still be starting).
for _ in $(seq 1 180); do
  if curl -fsS -o /dev/null --max-time 2 "$URL"; then break; fi
  sleep 1
done

# Disable X11 blanking if an X server exists (harmless no-op on Wayland).
if command -v xset >/dev/null 2>&1; then
  xset s off 2>/dev/null || true
  xset s noblank 2>/dev/null || true
  xset -dpms 2>/dev/null || true
fi

BROWSER="$(command -v chromium || command -v chromium-browser || true)"
[ -z "$BROWSER" ] && { log "no chromium found"; exit 1; }

SCALE="${PLANTER_SCALE:-1.0}"
OZONE="${PLANTER_OZONE:-auto}"

# Work out the display environment. When launched from a session autostart the
# vars are present; when launched from a service/SSH they are not, so derive them
# from the running user's runtime dir (the socket is wayland-0, wayland-1, ...).
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
  for s in "$XDG_RUNTIME_DIR"/wayland-*; do
    [ -S "$s" ] || continue
    case "$s" in *.lock) continue;; esac
    export WAYLAND_DISPLAY="$(basename "$s")"
    break
  done
fi
if [ "$OZONE" = "auto" ]; then
  if [ -n "${WAYLAND_DISPLAY:-}" ] && [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]; then
    OZONE="wayland"
  else
    OZONE="x11"
  fi
fi
log "display env: WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-none} DISPLAY=${DISPLAY:-none} ozone=$OZONE"

launch() {
  "$BROWSER" \
    --kiosk \
    --touch-events=enabled \
    --overscroll-history-navigation=0 \
    --enable-features=TouchpadOverscrollHistoryNavigation:disabled \
    --ozone-platform="$OZONE" \
    --enable-features=UseOzonePlatform \
    --disable-features=Translate,TranslateUI,MediaRouter,OptimizationHints \
    --disable-translate \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --lang=de \
    --accept-lang=de-DE,de \
    --force-device-scale-factor="$SCALE" \
    --no-first-run \
    --password-store=basic \
    --check-for-update-interval=31536000 \
    --user-data-dir="$PROFILE" \
    "$URL"
}

# Relaunch forever if Chromium exits unexpectedly.
while true; do
  log "starting chromium (ozone=$OZONE, scale=$SCALE) -> $URL"
  launch >>"$LOG" 2>&1
  rc=$?
  log "chromium exited rc=$rc; restarting in 5s"
  sleep 5
done
