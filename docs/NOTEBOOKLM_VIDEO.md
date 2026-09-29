# NotebookLM — video presentation guide (FloraHome)

This file tells you exactly what to feed **NotebookLM** and what to type so it
produces a great **Video Overview** of FloraHome, plus a screen‑demo script for
the live pitch.

---

## 1) Create the notebook and add sources

In NotebookLM: **New notebook → Add sources → Upload / paste**. Add these repo
files (uploads are best, so it reads the real text):

- `README.md` *(top‑level pitch + the dashboard screenshot)*
- `docs/ARCHITECTURE.md` *(how the pieces fit, with diagrams)*
- `docs/AI.md` *(FlorAI: tools, safety rails)*
- `docs/MQTT.md` *(per‑plant namespaces)*
- `docs/CALIBRATION.md` *(soil sensor calibration)*
- `docs/WIRING_FOR_DUMMIES.md` *(the physical rig)*
- `docs/HOMEASSISTANT.md` *(HA discovery)*
- `DEPLOYMENT_HANDOFF.md` *(real Pi + ESP32 deployment log)*
- `docs/HANDOFF_NEXT_SESSION.md` *(latest status)*
- `api/plants.json` *(the species care database)*
- `docker-compose.yml` *(the stack)*
- `esphome/plant-a.yaml` *(a node config)*
- Optional image: `docs/img/florahome-dashboard-live.png`

> Tip: also paste the "Key facts" section (below) as a source so the numbers are
> exact.

---

## 2) Generate the Video Overview

Click **Video Overview → Customize** and paste this prompt:

```
Create a ~4-minute video overview of FloraHome for a hackathon jury and a
non-technical audience.

Style: energetic, friendly, clear. Short sentences. Explain jargon as you go
(MQTT, ESP32, soil-moisture ADC). Use "we" to describe our build.

Cover, in this order:
1. The problem: houseplants get over/under-watered; cloud plant gadgets lock you
   into accounts and don't act per plant.
2. What we built: one Raspberry Pi + ESP32 nodes, fully self-hosted, no cloud.
   Each plant is treated individually — its own MQTT namespace, care range,
   alerts, history and a live "mood".
3. The physical rig: ESP32, capacitive soil probe, DHT11, LDR, relay driving a
   12 V diaphragm pump and grow light, buzzer, button. Show that a dose is
   pumped by volume (mL/s flow rate), not a blind timer.
4. Safety first: pump relay is OFF on boot and every dose is clamped in firmware;
   daily cap, cooldown, over-water guard and EMERGENCY STOP.
5. FlorAI: a floating assistant that reads live telemetry and can actually
   control the plant (with off/ask/auto levels) using tools — read state, set
   watering/light, apply species care, water now, write a diary.
6. Add-a-plant from a photo: snap a plant, a vision model identifies the species
   and proposes care values from our curated plant database.
7. Dashboard: per-plant cards, moods, a graph history, an in-app "how it works",
   a one-button DEMO mode, and a plant diary written as 3 short bullets a week.
8. Why it's different: data you own, per-plant autonomy, works offline on the
   LAN, plugs into Home Assistant.

End with a one-line call to action: "FloraHome — a self-hosted smart planter
that finally treats every plant like an individual."
```

Keep it factual; if a number isn't in the sources, don't invent one.

---

## 3) Key facts (paste as a source so the numbers are right)

- **Stack:** Raspberry Pi (Docker Compose) + ESP32 nodes over Wi‑Fi.
- **Services:** FastAPI dashboard/API, Mosquitto MQTT, InfluxDB + Telegraf
  (history), nginx (web), ESPHome (flash/OTA), on a single Pi. No cloud account.
- **Per‑plant:** topic namespace `planter/<node>/…`, own rules, cooldown, daily
  cap, alerts, history, Home Assistant device.
- **Sensing:** capacitive soil probe (ADC, power‑gated to fight corrosion),
  DHT11 temp/humidity (10 kΩ pull‑up), LDR light, optional tank/battery.
- **Actuators:** 12 V diaphragm pump via relay, grow light via relay, buzzer,
  water‑now button.
- **Dosing by volume:** `dose = min(deficit% × pot_ml, pump_max_ml) ÷ pump_ml_per_s`
  → e.g. a 30 % deficit ≈ 120 mL ≈ a few seconds, hard‑capped per dose.
- **AI:** OpenAI‑compatible endpoint (Google Gemini `gemini-3.5-flash` used for
  chat + vision); tool‑calling with hard safety rails; graceful fallbacks
  (plain answer → local sensor‑grounded answer) bounded to ~30 s.
- **Add from photo:** image → species + proposed care, matched against the curated
  plant database (`api/plants.json`, 17+ species).
- **DEMO mode:** one switch seeds 5 example plants with varied readings, moods,
  36 h of history and a rich diary — no hardware needed.
- **i18n:** English (default), German, Dutch.

---

## 4) Screen‑demo script (live, ~2–3 min)

1. **Backend → 🎬 Demo mode ON.** Watch 5 plants appear with different moods.
2. **Overview:** point out the plant tiles, the mood, the big soil %, and the
   **FlorAI** card. Tap a quick‑action chip ("How is it?").
3. **Diary:** show a plant's diary = **3 tidy bullets** for the week.
4. **History tab:** show a smoothed graph (moisture over 24 h).
5. **Add from photo:** Plants → 📷 New plant → take a plant photo → show it
   identifying the species + proposing care → Create.
6. **Safety:** Control tab → point at **EMERGENCY STOP** and the Watering settings
   (dose cap, cooldown, daily cap).
7. **🌱 Demo mode OFF** to return to the real plants — done.

---

## 5) Talking points to hit (optional voice‑over)

- "Every tray of plants is a different ecosystem — we model that per plant."
- "If the Pi dies, the pump still can't run away — the firmware is the backstop."
- "We own the data; it lives on the Pi, not someone's cloud."
- "Adding a plant is a photo and one tap."
