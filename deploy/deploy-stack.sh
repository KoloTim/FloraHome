#!/usr/bin/env bash
# Deploy the Smart Planter stack into $HOME/smartplanter on a fresh host.
# Assumes: Docker installed, the source tree already copied to ~/smartplanter-src.tgz
# and a working .env placed at ~/smartplanter.env.
set -euo pipefail

mkdir -p "$HOME/smartplanter"
tar xzf "$HOME/smartplanter-src.tgz" -C "$HOME"
mv -f "$HOME/smartplanter.env" "$HOME/smartplanter/.env"
cd "$HOME/smartplanter"

# Data dir must be writable by the API container user (PUID/PGID, default 1000).
sudo chown -R "${PUID:-1000}:${PGID:-1000}" data 2>/dev/null || true

docker compose up -d --build
sleep 10
# Ensure the API is actually up after the data-dir fix.
docker compose up -d --force-recreate api

echo "==> Waiting for API /health ..."
for i in $(seq 1 60); do
  if curl -fsS http://localhost:8097/health >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS http://localhost:8097/health || { echo "API did not come up:"; docker compose logs --tail=40 api; exit 1; }

docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
echo
echo "Dashboard http://<host>:8098   API :8097   Grafana :3030   Influx :8086"
