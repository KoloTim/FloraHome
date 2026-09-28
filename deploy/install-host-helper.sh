#!/usr/bin/env bash
# Install the FloraHome host helper as a root systemd service on the Pi.
# Reads HOST_HELPER_TOKEN from the stack's .env (or generates one).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO/.env"

TOKEN="$(grep -E '^HOST_HELPER_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
if [ -z "$TOKEN" ]; then
  TOKEN="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
  echo "HOST_HELPER_TOKEN=$TOKEN" >> "$ENV_FILE"
  echo "generated HOST_HELPER_TOKEN and appended to .env"
fi

sudo install -m 0755 "$REPO/deploy/host-helper.sh" /usr/local/bin/florahome-host-helper.sh

sudo tee /etc/systemd/system/florahome-host-helper.service >/dev/null <<EOF
[Unit]
Description=FloraHome host helper (hotspot + USB flash shim)
After=network-online.target

[Service]
Type=simple
Environment=HOST_HELPER_TOKEN=$TOKEN
Environment=HOST_HELPER_PORT=6054
Environment=HOST_HELPER_BIND=0.0.0.0
Environment=ESP_HOME=$REPO/esphome
# Allow the helper port only from the docker bridge, not the LAN.
ExecStartPre=/bin/sh -c 'iptables -C INPUT -p tcp --dport 6054 -s 172.16.0.0/12 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 6054 -s 172.16.0.0/12 -j ACCEPT; iptables -C INPUT -p tcp --dport 6054 ! -s 172.16.0.0/12 -j DROP 2>/dev/null || iptables -I INPUT -p tcp --dport 6054 ! -s 172.16.0.0/12 -j DROP'
ExecStart=/usr/local/bin/florahome-host-helper.sh
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now florahome-host-helper.service
sleep 2
systemctl --no-pager --full status florahome-host-helper.service | head -8
echo
echo "Token is in .env as HOST_HELPER_TOKEN (the API reads it too)."
echo "Test:  curl -s -H "X-Host-Token: \$HOST_HELPER_TOKEN" http://127.0.0.1:6054/health"
