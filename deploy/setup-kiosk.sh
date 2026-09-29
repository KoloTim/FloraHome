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

# 3. Autostart on desktop login.
#    The labwc session runs /usr/bin/lxsession-xdg-autostart, which honours
#    ~/.config/autostart/*.desktop.
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/planter-kiosk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=FloraHome Kiosk
Comment=Full-screen plant dashboard
Exec=$HERE/kiosk.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

# 4. labwc session autostart (the reliable hook on Pi OS Bookworm/Trixie, where
#    the XDG autostart list is not always run). Harmless if the file exists.
mkdir -p "$HOME/.config/labwc"
MARK="# FloraHome kiosk"
if ! grep -qF "$MARK" "$HOME/.config/labwc/autostart" 2>/dev/null; then
  printf '%s\n' "$MARK" "$HERE/kiosk.sh &" >> "$HOME/.config/labwc/autostart"
fi

# 5. Watchdog: a systemd user service that (re)starts the kiosk and keeps it
#    running even if Chromium crashes. Enabled for the desktop user.
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/florahome-kiosk.service" <<EOF
[Unit]
Description=FloraHome kiosk
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=$HERE/kiosk.sh
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
