#!/usr/bin/env bash
# FloraHome calibration helper — run ON the Pi, from the repo root:
#
#   bash deploy/calibrate.sh watch              # live raw values
#   bash deploy/calibrate.sh soil               # guided 2-point soil calibration
#   bash deploy/calibrate.sh soil-set 2.55 1.15 # set the two voltages directly
#
# Soil calibration is applied at runtime over MQTT and persisted on the node,
# so no re-flash is needed.
set -euo pipefail

# cd to repo root if this script sits in deploy/
cd "$(dirname "$0")/.." 2>/dev/null || true
[ -f .env ] && set -a && . ./.env && set +a

NET="smartplanter_default"
MQTT_USER="${MQTT_USER:-planter}"
MQTT_PASSWORD="${MQTT_PASSWORD:-}"

MOSQ() { docker run --rm --network "$NET" eclipse-mosquitto:2 "$@"; }

field_avg() { # field seconds -> prints average (or "-")
  local field="$1" secs="${2:-12}"
  MOSQ timeout "$secs" mosquitto_sub -h mosquitto -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
      -t planter/telemetry -v 2>/dev/null \
  | FIELD="$field" python3 -c '
import sys, json, os
f = os.environ["FIELD"]; vals = []
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("planter/telemetry"):
        continue
    try:
        d = json.loads(line.split(" ", 1)[1])
    except Exception:
        continue
    if isinstance(d.get(f), (int, float)):
        vals.append(d[f])
print("%.3f" % (sum(vals) / len(vals)) if vals else "-")
'
}

set_cal() { # dry wet
  MOSQ mosquitto_pub -h mosquitto -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t planter/cmd \
    -m "{\"action\":\"cal\",\"soil_dry_v\":$1,\"soil_wet_v\":$2}"
  echo "   sent cal dry=$1 wet=$2"
}

case "${1:-watch}" in
  watch)
    echo "soil_v   ldr_v   moisture_pct   lux   (cal_dry, cal_wet)   -- Ctrl-C to stop"
    MOSQ mosquitto_sub -h mosquitto -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t planter/telemetry -v \
    | python3 -u -c '
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("planter/telemetry"):
        continue
    try:
        d = json.loads(line.split(" ", 1)[1])
    except Exception:
        continue
    print("soil_v=%-7s ldr_v=%-7s moisture=%-6s lux=%-6s cal=(%s, %s)" % (
        d.get("soil_v"), d.get("ldr_v"), d.get("moisture_pct"), d.get("lux"),
        d.get("cal_dry"), d.get("cal_wet")))
'
    ;;

  soil)
    echo "== SOIL CALIBRATION (2-point) =="
    read -r -p "1) Probe in AIR (out of soil), then press Enter... " _
    echo "   sampling soil_v for 12s..."
    DRY=$(field_avg soil_v 12)
    echo "   dry = ${DRY} V"
    read -r -p "2) Probe in a GLASS OF TAP WATER, then press Enter... " _
    echo "   sampling soil_v for 12s..."
    WET=$(field_avg soil_v 12)
    echo "   wet = ${WET} V"
    if [ "$DRY" = "-" ] || [ "$WET" = "-" ]; then
      echo "!! no readings — is the node online and the probe wired? (AOUT->GPIO34, VCC->GPIO25)"
      exit 1
    fi
    set_cal "$DRY" "$WET"
    echo "   waiting for the next readings..."
    sleep 12
    echo "   moisture_pct now = $(field_avg moisture_pct 12)"
    ;;

  soil-set)
    set_cal "${2:?need dry}" "${3:?need wet}"
    ;;

  *)
    echo "usage: $0 [watch|soil|soil-set <dry_v> <wet_v>]"; exit 1;;
esac
