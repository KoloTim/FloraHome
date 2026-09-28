#!/usr/bin/env bash
# Seeds 24h of plausible history into InfluxDB so the demo chart is never empty.
#
#   ./scripts/seed_demo_data.sh           # add to whatever is already there
#   ./scripts/seed_demo_data.sh --purge   # wipe the bucket first, then seed
#
# Use --purge before the demo so test readings are not mixed into the nice curve.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a

BUCKET="${INFLUX_BUCKET:-planter}"
ORG="${INFLUX_ORG:-kololab}"

if [ "${1:-}" = "--purge" ]; then
  echo "==> Purging everything in bucket '${BUCKET}' (from 1970 to now)"
  docker exec planter-influxdb influx delete \
    --bucket "$BUCKET" --org "$ORG" --token "$INFLUX_TOKEN" \
    --start 1970-01-01T00:00:00Z --stop "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "    done"
fi

echo "==> Seeding 24h of demo history into '${BUCKET}'"
python3 - <<'PY' | docker exec -i planter-influxdb influx write \
    --bucket "$BUCKET" --org "$ORG" --token "$INFLUX_TOKEN" --precision s -
import math, random, time

now = int(time.time())
random.seed(7)
STEP = 10                      # one point every 10 s
N = 24 * 3600 // STEP
lines = []

WATER_CYCLE = 8 * 3600         # the pump fires roughly every 8 h
start_phase = (now - N * STEP) % WATER_CYCLE

for i in range(N):
    t = now - (N - i) * STEP
    hod = ((t % 86400) / 3600 + 1) % 24          # ~local hour (CEST)

    # Soil dries from ~70% down to ~26%, then jumps back up when the pump runs.
    frac = ((start_phase + i * STEP) % WATER_CYCLE) / WATER_CYCLE
    if frac < 0.85:
        moisture = 70 - (frac / 0.85) * 44
    else:
        moisture = 26 + ((frac - 0.85) / 0.15) * 15.4   # water spreading through soil
    moisture += random.uniform(-0.8, 0.8)

    # Daylight: smooth bell between 06:00 and 20:00, dark otherwise.
    lux = (22000 * math.sin(math.pi * (hod - 6) / 14) + random.uniform(-250, 250)) if 6 < hod < 20 else random.uniform(0, 12)

    temp = 18 + 6.5 * math.sin(math.pi * (hod - 8) / 15) + random.uniform(-0.25, 0.25)
    hum = 60 - 11 * math.sin(math.pi * (hod - 8) / 15) + random.uniform(-0.8, 0.8)
    tank = max(6, 94 - i * 0.0032 + random.uniform(-0.15, 0.15))
    rssi = -56 + random.uniform(-4, 4)

    lines.append(
        f"planter,device=smartplanter "
        f"moisture_pct={moisture:.1f},temp_c={temp:.1f},humidity={hum:.0f},"
        f"lux={lux:.0f},tank_pct={tank:.0f},rssi={rssi:.0f} {t}"
    )

print("\n".join(lines))
PY

echo "==> Done."
echo "    Dashboard: http://localhost:${WEB_PORT:-8098}"
echo "    Grafana:   http://localhost:${GRAFANA_PORT:-3030}"
