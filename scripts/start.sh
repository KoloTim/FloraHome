#!/usr/bin/env bash
# Brings the whole stack up, waits for health, runs a smoke test, and prints
# the URLs. Safe to run twice.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "ERROR: .env missing. Copy .env.example to .env and fill it in."; exit 1; }
set -a; . ./.env; set +a

WEB="http://localhost:${WEB_PORT:-8098}"
API="http://localhost:8097"

echo "==> Starting stack (this may build the API image the first time)…"
docker compose up -d --build

echo "==> Waiting for the API to answer /health (max 120s)…"
for i in $(seq 1 60); do
  if curl -fsS "$API/health" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS "$API/health" | python3 -m json.tool || { echo "API did not come up. Recent logs:"; docker compose logs --tail=40 api; exit 1; }

echo
echo "==> Containers:"
docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'

cat <<EOF

  Dashboard     ${WEB}
  API docs      ${API}/docs
  API health    ${API}/health
  Grafana       http://localhost:${GRAFANA_PORT:-3030}   (admin / \$GRAFANA_ADMIN_PASSWORD)
  InfluxDB      http://localhost:${INFLUX_PORT:-8086}
  MQTT          tcp://localhost:${MQTT_PORT:-1883}   user: \$MQTT_USER
  ESPHome       docker compose --profile tools up -d  ->  http://localhost:6052

Next steps:
  1. Test without hardware:  ./scripts/fake_node.sh
  2. Seed a full demo chart: ./scripts/seed_demo_data.sh
  3. Flash the ESP32:        cd esphome && cp secrets.yaml.example secrets.yaml && esphome run smartplanter.yaml
EOF
