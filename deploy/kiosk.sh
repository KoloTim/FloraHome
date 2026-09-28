#!/usr/bin/env bash
# FloraHome kiosk launcher: wait for the dashboard, then run Chromium full screen.
# Installed as an autostart entry by deploy/setup-kiosk.sh.
#
# Works on the Raspberry Pi OS "labwc" (Wayland) session: Chromium is forced onto
# the Wayland ozone platform, the translate bubble is suppressed, and the UI is
# scaled slightly so the 800x480 panel shows the whole overview.
set -u
URL="${PLANTER_URL:-http://localhost:8098/}"
PROFILE="$HOME/.config/planter-kiosk"
mkdir -p "$PROFILE"

# Wait up to 120 s for the dashboard (the Docker stack may still be starting).
for _ in $(seq 1 120); do
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
[ -z "$BROWSER" ] && { echo "kiosk: no chromium found"; exit 1; }

# UI scale: 1.0 is the recommended value for the 800x480 panel (bigger text,
# fewer surprises for a single-touch finger). Override with PLANTER_SCALE.
SCALE="${PLANTER_SCALE:-1.0}"

exec "$BROWSER" \
  --kiosk \
  --touch-events=enabled \
  --overscroll-history-navigation=0 \
  --enable-features=TouchpadOverscrollHistoryNavigation:disabled \
  --ozone-platform=wayland \
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
