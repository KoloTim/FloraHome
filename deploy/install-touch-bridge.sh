#!/usr/bin/env bash
# Install the FloraHome touch bridge as a systemd service.
# Run ON the Pi, as the desktop user (uses sudo). Requires python3 (already there).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/touch_bridge.py"
[ -f "$SCRIPT" ] || { echo "missing $SCRIPT"; exit 1; }

# uinput module + permissions
echo 'timtimtim' | sudo -S -p '' modprobe uinput 2>/dev/null || true
echo "uinput" | sudo tee /etc/modules-load.d/florahome-uinput.conf >/dev/null

sudo install -m 0755 "$SCRIPT" /usr/local/bin/florahome-touch-bridge.py

sudo tee /etc/systemd/system/florahome-touch.service >/dev/null <<'EOF'
[Unit]
Description=FloraHome touch bridge (absolute-mouse panel -> touchscreen)
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/florahome-touch-bridge.py --max-x 800 --max-y 480
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now florahome-touch.service
sleep 2
systemctl --no-pager --full status florahome-touch.service | head -12
echo
echo "Verify a touchscreen appears:"
echo "  libinput list-devices | grep -iA2 touch"
