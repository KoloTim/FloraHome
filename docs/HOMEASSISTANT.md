# Home Assistant integration

FloraHome exposes every plant to Home Assistant over MQTT, using HA's **MQTT
discovery**. No custom component or YAML required — point HA at the broker and
each plant appears as its own device.

## 1. Requirements

- Home Assistant with the **MQTT** integration (Settings → Devices & Services).
- The broker is the Pi: host `192.168.91.68`, port `1883`, user `planter`,
  password = `MQTT_PASSWORD` (from `~/smartplanter/.env`).

> If HA is not on the same network as the Pi, bridge them or use the Pi's IP on
> your LAN. The nodes themselves live on the Pi's `FloraHome` hotspot; HA only
> ever talks to the **broker**, so location does not matter.

## 2. Configure the MQTT integration

1. Settings → Devices & Services → **Add Integration → MQTT**.
2. Broker: `192.168.91.68`, Port `1883`, Username `planter`,
   Password `<MQTT_PASSWORD>`.
3. Leave **Enable discovery** on (default).

Within a few seconds the API's retained discovery messages create **one device
per node** (`plant-a`, `plant-b`, …) with:

| Entity | Type | Notes |
|---|---|---|
| Bodenfeuchte | sensor | % · device_class moisture |
| Temperatur | sensor | °C |
| Luftfeuchte | sensor | % |
| Licht | sensor | lx (relative) |
| Boden Rohspannung | sensor | V (diagnostic, for calibration) |
| Pumpe | sensor | `watering` / `idle` |
| WLAN-Signal | sensor | dBm (diagnostic) |
| Letztes Signal | sensor | timestamp (diagnostic) |
| Laufzeit | sensor | s (diagnostic) |
| **Jetzt bewässern** | button | sends a 20 s dose |
| **Buzzer testen** | button | 3 s beep |
| **Grow Light** | switch | on/off |

Availability is driven by `planter/<node>/state/online`, so entities go
*unavailable* when a node drops off.

## 3. Point HA at a command topic

The controls publish to `planter/<node>/cmd`. The water button sends:

```json
{"action": "pump", "seconds": 20, "reason": "ha"}
```

The firmware clamps this to `pump_max_seconds` (45 s) regardless of what is sent.

## 4. Automations (examples)

```yaml
# Heavy rain? skip the dose (steal the logic from HA's weather, not the plant)
automation:
  - alias: "FloraHome: water plant-a when needed"
    trigger:
      - platform: numeric_state
        entity_id: sensor.plant_a_bodenfeuchte
        below: 28
        for: "00:20:00"
    action:
      - action: button.press
        target: { entity_id: button.plant_a_jetzt_bewaessern }
    mode: single

  - alias: "FloraHome: shout if a node is silent"
    trigger:
      - platform: state
        entity_id: sensor.plant_a_letztes_signal
        to: "unavailable"
        for: "00:05:00"
    action:
      - action: notify.persistent_notification
        data: { message: "plant-a is offline" }
```

## 5. Manual YAML (no discovery)

If discovery is disabled, define entities yourself — same topics:

```yaml
mqtt:
  sensor:
    - name: "Plant A moisture"
      unique_id: plant_a_moisture
      state_topic: "planter/plant-a/state/moisture_pct"
      availability_topic: "planter/plant-a/state/online"
      unit_of_measurement: "%"
      device_class: moisture
      state_class: measurement
  button:
    - name: "Plant A water"
      unique_id: plant_a_water
      command_topic: "planter/plant-a/cmd"
      payload_press: '{"action":"pump","seconds":20,"reason":"ha"}'
  switch:
    - name: "Plant A grow light"
      unique_id: plant_a_light
      state_topic: "planter/plant-a/state/light"
      command_topic: "planter/plant-a/cmd"
      payload_on: '{"action":"light","state":"on"}'
      payload_off: '{"action":"light","state":"off"}'
      state_on: "on"
      state_off: "off"
```

## 6. Remove a node from HA

Every discovery config has a `unique_id`; to remove a node, publish an empty
retained payload to its config topics (or delete the device in HA). The API
publishes configs under `homeassistant/{sensor,button,switch}/planter_<node>/…`.

## 7. Roadmap

- Optional HACS component that manages nodes/plants and pulls species care data
  from OpenPlantbook alongside MQTT discovery.
