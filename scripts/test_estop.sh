#!/usr/bin/env bash
# Verifies the Not-Aus (emergency stop) end to end, without hardware.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

# NOTE: `timeout`/background cannot call a shell function, so docker is inlined.
PUB() {
  docker run --rm --network smartplanter_default eclipse-mosquitto:2 \
    mosquitto_pub -h mosquitto -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
    -t planter/telemetry -m "$1"
}
reading() {   # $1 moisture  $2 pump  $3 uptime_s
  printf '{"device":"smartplanter","fw":"1.0.0","ts":%d,"moisture_pct":%s,"temp_c":21.0,"humidity":57,"lux":8300,"tank_pct":78,"rssi":-58,"pump":"%s","mode":"auto","fault":"ok","on_s":%s}' \
    "$(date +%s)" "$1" "$2" "$3"
}

echo "=== 0) log in"
curl -fsS -c /tmp/estop.jar -X POST http://localhost:8097/api/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}" >/dev/null
echo "    ok"

COUNT_BEFORE=$(curl -fsS http://localhost:8097/api/state | python3 -c "import json,sys;print(json.load(sys.stdin)['pump_count_today'])")

echo "=== 1) subscribe to planter/cmd and planter/estop"
: > /tmp/estop_sub.out
docker run --rm --network smartplanter_default eclipse-mosquitto:2 \
  timeout 28 mosquitto_sub -h mosquitto -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
  -t 'planter/cmd' -t 'planter/estop' -v > /tmp/estop_sub.out 2>&1 &
SUB=$!
sleep 6

echo "=== 2) set NOT-AUS"
curl -fsS -b /tmp/estop.jar -X POST http://localhost:8097/api/estop | python3 -m json.tool
sleep 3

echo "=== 3) publish a VERY dry reading — auto watering must NOT fire"
PUB "$(reading 5.0 idle 500)"
sleep 11
curl -fsS http://localhost:8097/api/state | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print('    halted:',d['halted'],'| pump:',d['pump'],'| doses today:',d['pump_count_today'])"

COUNT_DURING=$(curl -fsS http://localhost:8097/api/state | python3 -c "import json,sys;print(json.load(sys.stdin)['pump_count_today'])")
if [ "$COUNT_DURING" = "$COUNT_BEFORE" ]; then
  echo "    ✅ PASS: no watering happened while NOT-AUS was set"
else
  echo "    ❌ FAIL: pump dose fired during NOT-AUS ($COUNT_BEFORE -> $COUNT_DURING)"
fi

echo "=== 4) clear NOT-AUS"
curl -fsS -b /tmp/estop.jar -X POST http://localhost:8097/api/resume | python3 -m json.tool
sleep 4

wait $SUB 2>/dev/null || true
echo "=== 5) broker saw:"
sed 's/^/    /' /tmp/estop_sub.out

echo "=== 6) audit trail (newest first):"
curl -fsS -b /tmp/estop.jar "http://localhost:8097/api/audit?limit=5" | python3 -c \
  "import json,sys; [print('   ',r['actor'],r['action'],'|',(r['detail'] or '')[:52]) for r in json.load(sys.stdin)]"
