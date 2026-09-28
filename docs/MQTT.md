# MQTT reference

Broker: `tcp://192.168.91.68:1883` (user `planter`). Every plant is a **node**
with its own topic namespace, so commands can never cross between plants.

## Per-node topics

| Topic | Direction | Payload |
|---|---|---|
| `planter/<node>/telemetry` | node → hub | JSON, every 10 s |
| `planter/<node>/status` | node → hub | `online` / `offline` (birth + LWT) |
| `planter/<node>/event` | node → hub | `{"event":"button_water_now"}` |
| `planter/<node>/cmd` | hub → node | `{"action":"pump\|light\|buzzer\|stop\|cal", …}` |
| `planter/<node>/estop` | hub → node | `{"estop":true\|false}` (retained) |
| `planter/<node>/state/<field>` | hub → HA | one retained value per field |

`<node>` is the ESPHome `device_name`, e.g. `plant-a`.

## Telemetry JSON keys

```
device, fw, ts,
moisture_pct, soil_v, temp_c, humidity, lux, ldr_v, tank_pct, rssi,
pump (watering|idle), light (on|off), mode, on_s, fault,
cal_dry, cal_wet
```

`fault` is a comma list of sensors returning NaN (`soil`, `dht11`, `ldr`,
`jsn_sr04t`) or `"ok"`. `tank_pct` is absent on nodes without a tank sensor.

## Commands (`planter/<node>/cmd`)

| Payload | Effect |
|---|---|
| `{"action":"pump","seconds":20}` | run the pump, clamped to `pump_max_seconds` (45 s) |
| `{"action":"stop"}` | stop any running dose, pump relay off |
| `{"action":"light","state":"on\|off"}` | grow light |
| `{"action":"buzzer","seconds":3}` | beep (≤30 s) |
| `{"action":"cal","soil_dry_v":2.5,"soil_wet_v":1.2}` | set soil calibration (persisted) |

`stop` and `estop` are **safety** paths; the rest are convenience.

## Node state for Home Assistant (retained)

`planter/<node>/state/{moisture_pct,temp_c,humidity,lux,soil_v,rssi,pump,
light,mode,fault,uptime,last_seen,online}`, plus the retained
`planter/<node>/estop`.

## Home Assistant discovery

The API publishes retained discovery configs:

```
homeassistant/sensor/planter_<node>/<key>/config
homeassistant/button/planter_<node>/{water,buzzer}/config
homeassistant/switch/planter_<node>/light/config
```

Each config carries a `device` block, so HA shows **one device per plant** with
sensors and controls. See [HOMEASSISTANT.md](HOMEASSISTANT.md).

## Test from the Pi

```bash
cd ~/smartplanter && set -a && . ./.env && set +a
# watch one node's telemetry
docker run --rm --network smartplanter_default eclipse-mosquitto:2 \
  mosquitto_sub -h mosquitto -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t 'planter/+/telemetry' -v
# send a 10 s dose to plant-a
docker run --rm --network smartplanter_default eclipse-mosquitto:2 \
  mosquitto_pub -h mosquitto -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
  -t planter/plant-a/cmd -m '{"action":"pump","seconds":10}'
```

## Migrating from the old single-topic scheme

Old (single-node, unsafe with 2+ plants) → new (per-node):

| Old | New |
|---|---|
| `planter/telemetry` | `planter/<node>/telemetry` |
| `planter/cmd` | `planter/<node>/cmd` |
| `planter/estop` | `planter/<node>/estop` |
| `planter/status` | `planter/<node>/status` |
| `planter/state/<f>` | `planter/<node>/state/<f>` |

After upgrading, clear the old retained topics once:

```bash
for t in planter/status planter/telemetry planter/cmd planter/estop planter/state; do
  mosquitto_pub -h <broker> -u planter -P "$MQTT_PASSWORD" -t "$t" -r -n
done
```
