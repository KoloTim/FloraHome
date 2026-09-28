# FloraHome — Architecture

> **Value proposition.** FloraHome turns a cheap ESP32 + a capacitive soil probe
> into a *self-hosted, cloud-free smart planter* that watches **each plant
> individually**, waters it on its own terms, keeps a history you own, alerts you
> when something is wrong, and plugs straight into **Home Assistant**. The
> firmware enforces the safety (never flood a plant, never run a pump dry); the
> Pi provides the comfort (when to water, when to shout, what to log). One Pi
> hosts many plants; adding a plant is one command.

Everything below runs on hardware you own. No vendor account, no cloud.

---

## 1. System overview

```mermaid
graph TB
  subgraph Plants["🌱 Plant nodes (ESP32, one per plant)"]
    A["plant-a<br/>ESP32 + DHT11 + LDR + HW-390<br/>relay → pump + grow light + buzzer"]
    B["plant-b<br/>…"]
    C["plant-c<br/>…"]
  end

  subgraph Pi["🍓 Raspberry Pi 4 (demo host)"]
    AP["FloraHome hotspot<br/>wlan0 10.42.0.1/24"]
    subgraph Docker["Docker Compose: smartplanter"]
      MQ["mosquitto<br/>MQTT broker :1883"]
      TG["telegraf<br/>MQTT → Influx"]
      IN["influxdb<br/>time-series :8086"]
      API["api (FastAPI)<br/>rules · alerts · audit · SSE :8097"]
      WEB["web (nginx)<br/>dashboard + Verlauf graphs + Flori :8098"]
      ESPH["esphome dashboard<br/>build + OTA :6052 (profile tools)"]
    end
    KIOSK["Chromium kiosk<br/>800×480 touch panel"]
  end

  U(("👤 You")) --> WEB
  U --> GRAF
  KIOSK --> WEB
  WEB --> API
  API --> MQ
  API --> TG
  TG --> IN
  GRAF --> IN
  ESPH -. "build / OTA" .-> A

  A & B & C <-->|MQTT over Wi-Fi| AP
  AP --- MQ

  API -. "Telegram alerts" .-> T(["📱 Telegram"])
  API --> HA["🏠 Home Assistant<br/>MQTT discovery"]
  HA --> MQ

  ESPH --- MQ
```

**Why it is safe.** The ESP32 holds the authority: the pump relay is forced off
on boot, a dose is clamped to `pump_max_seconds` (45 s) *in firmware*, and a
second command cannot extend a running dose (`mode: single`). If the Pi dies
mid-watering the relay still opens. The Pi's failure mode is "the plant goes
thirsty", never "the plant drowns".

---

## 2. Service map

| Container | Image | Role | Port |
|---|---|---|---|
| `planter-mosquitto` | eclipse-mosquitto:2 | authenticated MQTT broker; one namespace per node | 1883 |
| `planter-influxdb` | influxdb:2.7 | time-series history (30 d) | 8086 |
| `planter-telegraf` | telegraf:1.32-alpine | subscribes `planter/+/telemetry`, writes Influx | – |
| `planter-api` | built from `api/` | per-node rules, alerts, audit, history, SSE | 8097 |
| `planter-web` | nginx:1.27-alpine | dashboard + Verlauf graphs + `/api` reverse proxy | 8098 |
| `planter-esphome` | esphome/esphome:2025.8 | build + **OTA-flash** nodes from a browser | 6052 |

Start the optional ESPHome dashboard with `docker compose --profile tools up -d`.

---

## 3. Data flows

### 3.1 Telemetry (node → hub)

```mermaid
sequenceDiagram
  participant N as ESP32 (plant-a)
  participant M as mosquitto
  participant T as telegraf
  participant I as influxdb
  participant A as api
  participant W as dashboard (SSE)
  participant H as Home Assistant

  loop every 10 s
    N->>M: planter/plant-a/telemetry {moisture,temp,hum,lux,pump,fault,…}
  end
  M->>T: same (wildcard planter/+/telemetry)
  T->>I: planter measurement, tag device=plant-a
  M->>A: same
  A->>A: normalise → NODES['plant-a'].metrics, freshness, restart detect
  A->>W: SSE {"type":"telemetry","node":"plant-a",…}
  A->>M: retained planter/plant-a/state/<field> (for HA)
  M->>H: HA MQTT discovery + retained state
```

### 3.2 Watering decision (hub → node)

```mermaid
sequenceDiagram
  participant A as api (rules_tick every 5 s)
  participant M as mosquitto
  participant N as ESP32 (plant-a)
  participant P as pump (relay)

  A->>A: moisture < threshold AND not halted AND cooldown ok AND daily cap ok
  A->>M: planter/plant-a/cmd {"action":"pump","seconds":20}
  M->>N: command
  N->>N: clamp to ≤45 s, mode:single
  N->>P: relay ON
  N->>P: relay OFF after 20 s (firmware timer)
  N->>M: telemetry pump="watering" → later "idle"
```

The **same path** is used by the dashboard's "Jetzt bewässern" button, the Home
Assistant button, and the physical "Wasser jetzt" button (which calls the local
script directly and publishes `…/event`).

### 3.3 Alerts

The Pi raises alerts it can only know centrally — `node_silent`, `soil_dry`,
`tank_empty`, `sensor_fault` — per node (codes are prefixed `plant-a:`), opens an
`events` row, writes the `audit` log, pushes an SSE event, and sends Telegram
(10-minute anti-spam per code). The node's own buzzer only covers what the node
can still see. See [ALERTS.md](ALERTS.md).

---

## 4. MQTT topic namespace (multi-node safe)

```mermaid
graph LR
  subgraph per-node["planter/&lt;node&gt;/…"]
    TE["telemetry  node→hub"]
    ST["status     node→hub (LWT/birth)"]
    EV["event      node→hub"]
    CM["cmd        hub→node"]
    ES["estop      hub→node (retained)"]
    SS["state/&lt;field&gt;  hub→HA (retained)"]
  end
  subgraph ha["homeassistant/…"]
    D["{sensor,button,switch}/planter_&lt;node&gt;/&lt;id&gt;/config"]
  end
```

Because every node has its **own** `cmd`/`estop` topic, a command to `plant-a`
can never water `plant-b`. This is the fix for the original single-topic design
that made a second plant unsafe. The API subscribes with wildcards
(`planter/+/telemetry`, `planter/+/status`) and derives the node name from the
topic. Full table: [MQTT.md](MQTT.md).

---

## 5. Hardware per node

```mermaid
graph LR
  subgraph ESP["ESP32-WROOM (3.3 V logic)"]
    G34["GPIO34 ← HW-390 AOUT (ADC1)"]
    G25["GPIO25 → probe power (sampled only)"]
    G27["GPIO27 ← DHT11 data (10k pull-up)"]
    G35["GPIO35 ← LDR divider"]
    G26["GPIO26 → relay IN1 (pump)"]
    G13["GPIO13 → relay IN2 (grow light)"]
    G14["GPIO14 → buzzer"]
    G04["GPIO4  ← 'water now' button"]
  end
  subgraph Power["Power — two separate buses"]
    B5L["5 V logic buck → ESP32 + relay coils"]
    B12["12 V PSU (2 A fuse) → relay COM/NO"]
  end
  B12 --> REL["relay module"]
  REL --> PUMP["R385 12 V pump"]
  REL --> LIGHT["12 V grow light"]
  B5L --> ESP
```

Rules that are non-negotiable (see [WIRING_FOR_DUMMIES.md](WIRING_FOR_DUMMIES.md)):
logic and 12 V are separate rails tied at one point; the relay coil is driven by
the module/transistor, never a GPIO; a 10 kΩ pull-down on GPIO26 stops boot
chatter; GPIO34 is **ADC1** (ADC2 is dead while Wi-Fi is on).

---

## 6. Deployment topology

```mermaid
graph TB
  subgraph LAN["Home LAN 192.168.91.0/24"]
    PI["Pi 192.168.91.68<br/>eth0"]
    PC["your PC"]
    HAS["Home Assistant"]
  end
  PI ---|wlan0 AP| AP["FloraHome hotspot 10.42.0.1/24<br/>WPA2, 2.4 GHz ch6"]
  AP --- N1["plant-a 10.42.0.10"]
  AP --- N2["plant-b 10.42.0.x"]
  PI --> FLASH["flasher agent (spool + esptool)<br/>USB /dev/ttyUSB*"]
  FLASH --> USB["ESP32 in boot mode"]
```

The Pi is the **access point** for the nodes: they join `FloraHome`, so they are
on the same L2 as the broker and **OTA works** (port 3232). The Pi's own services
are reachable from the LAN. See [DEPLOYMENT_HANDOFF.md](../DEPLOYMENT_HANDOFF.md).

---

## 7. Home Assistant integration

The API publishes, per node, retained clarity topics under
`homeassistant/{sensor,button,switch}/planter_<node>/…`, so HA discovers **one
device per plant** with sensors (moisture, temperature, humidity, light, RSSI,
pump state, uptime) plus controls:

- **Button** "Jetzt bewässern" → `planter/<node>/cmd`
- **Button** "Buzzer testen"
- **Switch** "Grow Light" → `planter/<node>/cmd`

Point your HA MQTT integration at the broker and the devices appear. Details and
a YAML fallback: [HOMEASSISTANT.md](HOMEASSISTANT.md).

---

## 8. Storage & data model

```mermaid
erDiagram
  CONFIG ||--o{ EVENTS : "none"
  CONFIG {
    text key PK
    text value
    real updated_at
  }
  EVENTS {
    int id PK
    real ts
    text level
    text code
    text message
    int active
    real cleared_ts
  }
  AUDIT {
    int id PK
    real ts
    text actor
    text ip
    text action
    text detail
  }
```

- **SQLite** (`data/planter.db`) — runtime config, alert events, audit trail.
- **InfluxDB** — numeric history, one `planter` measurement tagged `device`.
- **In-memory** — last known state per node + a bounded fallback history.

Every write (login, config change, manual command, alert raise/clear) is written
to `audit` with actor + IP + timestamp.

---

## 9. Security model

- Dashboard is **read-only until login**; every `PUT`/`POST` needs an
  HMAC-signed session cookie (12 h).
- Login is rate-limited (10 failures from one IP → 5-minute lockout).
- API keys are stored masked; they can be rotated from the Backend tab and apply
  without a restart. The history graphs are rendered by the dashboard itself, so
  no separate Grafana container is shipped any more.
- Secrets (`esphome/secrets.yaml`, `.env`, `data/`) are gitignored; compiled
  firmware contains Wi-Fi/MQTT passwords and is never committed.

---

## 10. Adding a plant

```bash
deploy/add-node.sh new plant-d "My Monstera"     # create esphome/plant-d.yaml
deploy/add-node.sh discover                     # which ESP is on which port
deploy/add-node.sh compile plant-d
deploy/add-node.sh flash  plant-d /dev/ttyUSB0  # first time: USB
# after it joined FloraHome:
deploy/add-node.sh ota    plant-d 10.42.0.x     # ever after: OTA
```

Then give it a profile (species, avatar, care thresholds) in the dashboard's
**Pflanzen** settings. Full tutorial: [TUTORIALS.md](TUTORIALS.md).

---

## 11. Diagrams

The Mermaid blocks above render on GitHub. Rendered **SVG** copies live in
[`diagrams/`](diagrams/) and are regenerated with
`python3 scripts/render_diagrams.py` (uses mermaid.ink, no local toolchain).
