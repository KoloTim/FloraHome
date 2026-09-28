#!/usr/bin/env bash
# Publishes a fake telemetry payload so the dashboard/chart/rules can be tested
# before the ESP32 exists. Usage:
#   ./scripts/fake_node.sh              # one dry reading (triggers rules)
#   ./scripts/fake_node.sh --loop       # a reading every 10s, values drifting
#   ./scripts/fake_node.sh --wet        # one healthy reading
#   ./scripts/fake_node.sh --tank-empty
#   ./scripts/fake_node.sh --silent     # publish once, then never again
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a

HOST="${MQTT_HOST:-localhost}"
PORT="${MQTT_PORT:-1883}"
ENV="${1:-}"

pub() {
  docker run --rm --network host eclipse-mosquitto:2 \
    mosquitto_pub -h "$HOST" -p "$PORT" -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
    -t planter/telemetry -m "$1"
}

payload() {
  local moist=$1 tank=$2 fault=$3
  printf '{"device":"planetest","fw":"fake","ts":%d,"moisture_pct":%s,"temp_c":21.4,"humidity":58,"lux":8400,"tank_pct":%s,"rssi":-61,"pump":"idle","mode":"auto","fault":"%s"}' \
    "$(date +%s)" "$moist" "$tank" "$fault"
}

case "$ENV" in
  --wet)        pub "$(payload 72 85 ok)";;
  --tank-empty) pub "$(payload 68 9 ok)";;
  --fault)      pub "$(payload 0 80 "dht22")";;
  --loop)
    i=0
    while true; do
      # drift moisture down, then "water" back up: exercises the full rule path
      m=$(( 70 - (i * 6) % 60 ))
      pub "$(payload "$m" 80 ok)"
      echo "sent moisture=${m}%"
      i=$((i+1)); sleep 10
    done;;
  *)            pub "$(payload 18 80 ok)"; echo "sent a DRY reading (18%) — watch for the alert";;
esac
