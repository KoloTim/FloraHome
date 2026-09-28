#!/usr/bin/env bash
# Configure the Pi to boot straight into the Smart Planter dashboard, full screen.
# Run ON the Pi, as the desktop user (no sudo needed except where noted).
set -euo pipefail

URL="${PLANTER_URL:-http://localhost:8098/}"
KIOSK_DIR="$HOME/.config/autostart"
mkdir -p "$KIOSK_DIR"

# Chromium binary name varies: chromium-browser (RPi OS) or chromium.
BROWSER="$(command -v chromium-browser || command -v chromium || true)"
if [ -z "$BROWSER" ]; then
  echo "Chromium not found; installing..."
  sudo apt-get update -qq && sudo apt-get install -y chromium-browser
  BROWSER="$(command -v chromium-browser || command -v chromium)"
fi
echo "Using browser: $BROWSER"

# Disable screen blanking at login.
cat > "$KIOSK_DIR/planter-noblank.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Planter no-blank
Exec=sh -c "xset s off; xset s noblank; xset -dpms"
X-GNOME-Autostart-enabled=true
EOF

# Launch the dashboard kiosk.
cat > "$KIOSK_DIR/planter-kiosk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Planter kiosk
Comment=Smart Planter dashboard full screen
Exec=$BROWSER --kiosk --noerrdialogs --disable-infobars --incognito --check-for-update-interval=31536000 $URL
X-GNOME-Autostart-enabled=true
EOF

echo
echo "Done. Enable desktop autologin so it starts by itself:"
echo "  sudo raspi-config   # System Options -> Boot/Auto Login -> Desktop Autologin"
echo
echo "Test it now (from the graphical session):"
echo "  $BROWSER --kiosk --incognito $URL"
