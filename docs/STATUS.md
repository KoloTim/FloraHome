# Status — live build log

_Last updated: 2026-09-29._

## Neu (Dashboard-/Resilienz-Session)

- **Flori**: animiertes Maskottchen als schwebender Begleiter (Clippy-Style) mit
  kontextuellen Pflanzentipps; antippbar (öffnet den Chat), verschiebbar,
  ausblendbar. Der Chat ist nach ganz oben in **Übersicht** gezogen, der separate
  **KI-Tab ist weg**.
- **Verlauf-Tab**: eigenes Diagramm-Dashboard (Bodenfeuchte, Temperatur,
  Luftfeuchte, Licht, Tank, Rohspannung, RSSI) direkt aus InfluxDB. **Grafana
  entfernt** (InfluxDB + Telegraf bleiben der Datenspeicher).
- **Knoten-Resilienz**: Offline-Erkennung jetzt konfigurierbar (Standard **90 s**
  statt hart 30 s), LWT/Birth wird respektiert, kein Wiederbeleben durch retained
  „online“ beim Reconnect, Regeln gießen nicht mehr auf veralteten Werten.
- **Tagebuch**: Erklärung + Einstellungen (automatische Einträge, Intervall in
  Tagen) pro Pflanze; Timer übersteht Neustarts.
- **Resistive-Touch**: größere Ziele, Press-Feedback, Phantom-Klick-Filter,
  20 px Drag-Totzone, echter Login-Dialog statt `prompt()`, NOT-AUS zweistufig.
- **Backend**: „API-Schlüssel & Zugänge“ (maskiert, zur Laufzeit änderbar,
  ohne Neustart) + „System & Status“-Karte.

## Done

- **Pi** (`Tim`, `192.168.91.68`): full stack; dashboard `:8098`, API `:8097`,
  InfluxDB `:8086`, Mosquitto healthy.
- **Multi-node**: `plant-a` (`10.42.0.10`, Monstera "Moni") and `plant-b`
  (`10.42.0.11`, Sansevieria "Sanse") on their own MQTT namespaces and Home
  Assistant devices. OTA works (Pi hotspot `FloraHome`).
- **Per-node settings**: watering/alert thresholds are now **per plant**
  (`/api/config/node/<node>`); choosing a species applies **smart defaults**
  derived from its care range.
- **AI** (Google `gemini-3.8-flash`, auto-fallback): grounded chat, photo →
  species → plant profile, and a **weekly plant diary** (per node, stored).
- **Touch**: absolute-mouse panel bridged to a real touchscreen (uinput);
  visible scrollbar, A−/A+ scale, tap ripple, 48px targets; kiosk autostarts.
- **Devices & flashing** from the dashboard via the host helper; hotspot edits
  apply for real now.
- **Battery** node template (deep sleep + battery ADC), `docs/BATTERY.md`.
- **Voice** scaffolding: 🎤 talk-to-plants button (mic → STT → AI → speaker),
  `docs/VOICE.md` (mic/speaker placement).

## Current sensor state

| Node | Reading | Verdict |
|---|---|---|
| plant-a | moisture 100 %, 20–22 °C, ~78 %, ~4000 lx, `fault: ok` | ✅ sensors wired, **needs soil calibration** |
| plant-b | only lux, `fault: dht11,ldr` | ⚠️ sensors not wired yet |

## Next actions

1. **Calibrate plant-a** (dashboard → Kalibrierung, per plant).
2. **Wire plant-b's sensors** (DHT11 → GPIO27, LDR divider → GPIO35, soil).
3. Plug a **USB mic + speaker** into the Pi for voice (`docs/VOICE.md`).
4. Optional: battery node end-to-end; weekly diary cron is automatic.

## Open issues

- `plant-b:sensor_fault` (`dht11,ldr`) is expected — no sensors wired.
- Touch panel is single-touch (hardware); bridged for native scroll, no pinch.
- Voice needs a USB mic (the Pi has no built-in input).

## Operations

```bash
cd ~/smartplanter
# per-node config
curl -s localhost:8097/api/config/node/plant-a | python3 -m json.tool
# flash a node from the Pi
deploy/add-node.sh discover && deploy/add-node.sh compile plant-c
deploy/add-node.sh flash plant-c /dev/ttyUSB0
# write a diary entry now
curl -s -b cookie.txt -X POST localhost:8097/api/diary/plant-a
```
