#!/usr/bin/env bash
# Install Docker Engine + compose plugin on a Debian/Raspberry Pi OS host.
# Run ON the Pi:   sudo bash deploy/install-docker-pi.sh
set -euo pipefail

if ! command -v curl >/dev/null; then
  apt-get update -qq && apt-get install -y -qq ca-certificates curl
fi

curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
sh /tmp/get-docker.sh

TARGET_USER="${SUDO_USER:-$USER}"
usermod -aG docker "$TARGET_USER"

systemctl enable --now docker
docker --version
docker compose version
echo "Added '$TARGET_USER' to the docker group. Log out/in (or new SSH session) for it to apply."
