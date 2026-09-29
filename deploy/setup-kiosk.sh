#!/usr/bin/env bash
# Configure the Pi to boot straight into the FloraHome dashboard, full screen.
# Works on the Raspberry Pi OS "labwc" (Wayland) desktop. Run as the desktop user
# (tim) from the repo root or deploy/ directory.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
URL="${PLANTER_URL:-http://localhost:8098/}"

BROWSER="$(command -v chromium || command -v chromium-browser || true)"
if [ -z "$BROWSER" ]; then
  echo "Chromium not found; installing..."
  sudo apt-get update -qq && sudo apt-get install -y chromium
  BROWSER="$(command -v chromium || command -v chromium-browser || true)"
fi
echo "Using browser: ${BROWSER:-NOT FOUND}"

# 1. Launcher (this repo's deploy/kiosk.sh), made executable.
chmod +x "$HERE/kiosk.sh"

# 2. Kill the Chromium translate bubble at the source (managed policy).
sudo mkdir -p /etc/chromium/policies/managed
printf '%s\n' '{"TranslateEnabled": false}' \
  | sudo tee /etc/chromium/policies/managed/florahome.json >/dev/null

# 3. Autostart on desktop login. Single source of truth: the labwc session
#    autostart, invoked via /bin/bash so it does NOT depend on the executable
#    bit surviving an rsync/deploy (the usual reason the kiosk "stops working").
mkdir -p "$HOME/.config/labwc"
AUTOSTART="$HOME/.config/labwc/autostart"
touch "$AUTOSTART"
grep -v 'smartplanter/deploy/kiosk.sh' "$AUTOSTART" > "$AUTOSTART.tmp" 2>/dev/null || true
printf '%s\n' "# FloraHome kiosk" "/bin/bash $HERE/kiosk.sh &" >> "$AUTOSTART.tmp"
mv "$AUTOSTART.tmp" "$AUTOSTART"

# 3b. Drop any XDG autostart entry — it would launch a second kiosk instance.
rm -f "$HOME/.config/autostart/planter-kiosk.desktop"

# 4. Watchdog: a systemd user service that (re)starts the kiosk if Chromium
#    crashes. Optional (the launcher already self-restarts); also bash-invoked.
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/florahome-kiosk.service" <<EOF
[Unit]
Description=FloraHome kiosk
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/bin/bash $HERE/kiosk.sh
Restart=always
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF

echo
echo "Installed kiosk launcher + autostart entry + labwc autostart + user service."
echo "To enable the watchdog:  systemctl --user enable --now florahome-kiosk.service"
echo "Enable desktop autologin once so it starts by itself:"
echo "  sudo raspi-config   # System Options -> Boot / Auto Login -> Desktop Autologin"
echo
echo "Test now (from the graphical session): $HERE/kiosk.sh"
