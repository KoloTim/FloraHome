# HACKATHON PLAYBOOK — 24 h, demo included

You are in the phase where **hardware is real and time is not**. This file is the
order of work, the demo script, and what to do when something breaks on stage.

---

## Rules that decide this build

1. **Freeze features 8 h before judging.** Everything after that is rehearsal and copy.
2. **The demo must never depend on real soil drying on schedule.** Trigger it by hand.
3. **Seed the chart before you walk up**, not while they watch.
4. **When something breaks, cut it.** A confident four-part demo beats a flaky six-part one.
5. **Charge everything, and bring the chargers.**

---

## Timeline

| Hours | Focus | Done when |
|---|---|---|
| 0–2 | Bench bring-up: raw sensor values, relay test | You can see a voltage you trust |
| 2–4 | ESP32 → MQTT → InfluxDB | **One reading visible in the DB** |
| 4–7 | Pump logic + the 45 s ceiling + NOT-AUS | The control test in `PREFLIGHT.md` §5 passes twice |
| 7–10 | Dashboard polish, alerts, Telegram | A dry reading puts a message on your phone |
| 10–13 | Login, read-only view, audit log | A judge cannot change a setting without logging in |
| 13–15 | Mechanical build, cable tidy, plant potted | The rig survives a nudge and a lift |
| 15–16 | **Feature freeze** | Stop adding things |
| 16–19 | Rehearsal ×3 on the real hardware, on battery, no reboot | Nothing surprises you on the fourth run |
| 19–21 | Slides, DE/NL copy, pitch practice | You can do the demo in 4 minutes flat |
| 21–24 | Buffer, plus the Pi moves to the real spot | – |

**Hard checkpoint at hour 4.** If one sensor reading is not in the database by hour 6,
cut the water-level sensor and the grow light immediately and go for the simple demo.

---

## The demo script (4 minutes, verbal + live)

Rehearse it in exactly this order. Each step is a beat.

**0. Cold open — say nothing, show the plant.**
Point at the live dashboard and the actual pot side by side. Let them see the
numbers match the plant. *"This is a plant that waters itself — and keeps a receipt."*

**1. The automatic decision.**
> *"Right now the soil is at 18 %. The rule is: below 30 %, water for 20 seconds.
> Nobody pressed anything."*

Pull the probe out and hold it in the air — or use the **force-dry** button on the
dashboard. The reading drops, the pump fires, the moisture line jumps on the chart.
**Rehearse the timing so the pump noise lands on your sentence.**

**2. The safety argument — this is your strongest point.**
> *"What if the Raspberry Pi dies mid-watering? It doesn't matter. The pump's maximum
> run time is enforced on the ESP32 itself, in firmware. The relay is off by default at
> boot. The Pi's job is deciding when; the microcontroller's job is making sure it
> cannot go wrong."*

Demonstrate: hold the "water now" button, let go, and let it hit the 45 s ceiling
**without** the Pi. Then unplug the Pi. The plant is still fine.

**3. The receipt — your CORE requirement.**
Log in, show the audit log: who watered, when, how much, which sensor went quiet.
> *"And you can't fake it: every command is stamped with a user and a time."*

**4. The alert.**
Unplug the DHT22 (or use `./scripts/fake_node.sh --fault`). Within a minute, a
Telegram lands on your phone in front of them. *"It tells me before the plant dies."*

**5. Close on the numbers.**
> *"Four sensors, one pump, no cloud, everything on our own hardware. The soil
> moisture trend over 24 hours in one line — and a full audit trail behind it."*

---

## If something breaks, say this

| What dies | What you say | What you do |
|---|---|---|
| Wi-Fi / venue network | *"The node buffers and republishes — let me show you the local view."* | Switch to the node's own web UI at `http://<node-ip>` |
| The Pi | *"This is the point. Watch."* | Unplug it. The plant keeps watering on its own rules. **This is a win, not a failure.** |
| InfluxDB / the chart | *"History aside — the live values and the pump are unaffected."* | Hide the chart panel, stay on live tiles |
| The DHT22 | *"And there's the alarm it's designed to catch."* | Show the fault alert; it *is* the demo |
| A sensor reads nonsense | *"Let's not trust that one."* | Disable that tile, stay on moisture |
| Total stack down | – | Restart the Pi, `fake_node.sh` on loop, run the demo from the dashboard's live feed |

**Do not debug on stage.** If a step fails twice, skip it and move on. Confidence
sells more than completeness.

---

## Judges' questions — have the answers ready

**"How do you know the readings are right?"**
> Two-point calibration on the bench: dry air and water, and the raw voltage is shown
> as its own diagnostic entity so anyone can check the maths.

**"What stops it flooding the plant?"**
> Three limits, and none of them live on the Raspberry Pi: relay off at boot, run time
> clamped to 45 s in firmware, and the dose is `mode: single` so a second command cannot
> extend one already running. Plus a cooldown and a daily cap in the software layer.

**"Is it secure? Anyone could water your plant."**
> The dashboard is read-only by default. Every write needs an HMAC session cookie,
> login is rate-limited, and every action lands in an immutable audit log with actor and IP.

**"What happens without internet?"**
> Nothing breaks. Everything is local: MQTT, database, dashboard. Only the Telegram
> notification needs the outside world.

**"How much energy does it use?"**  *(Green track — say the numbers)*
> The pump runs ~20 s a few times a day, so a few watt-hours a week. The largest saving
> is the soil probe: it is powered only for the 3 seconds it actually measures, which
> cuts both its consumption and its corrosion. Alerting only on real state changes avoids
> spamming a network with 17 000 pointless notifications a day.

**"Where does the water go?"**
> It is a closed loop: the tank feeds the pot, the soil probe decides, and the trend line
> shows the water actually arriving. The tank level is measured with an ultrasonic sensor,
> and if it empties you get told before the soil does.

**"Why not use an Arduino?"**
> It is an ESP32: Wi-Fi, OTA updates, and the pump's safety limits run on it locally.
> The Pi is a convenience layer. If the Pi is unplugged the plant still waters itself.

---

## First 90 minutes if everything is on fire

1. `cd /opt/stacks/smartplanter && docker compose up -d` — is the stack up?
2. `./scripts/fake_node.sh --loop` — do the tiles and the chart move?
3. `./scripts/seed_demo_data.sh` — is the history there?
4. Still no hardware? **Demo from the fake node.** The software story is complete without
   a single real sensor, and you can still show the pump logic, the audit log and the alerts.

That fallback is legitimate. Say so up front if you have to use it, and spend the
remaining time making the story tighter instead of the wiring shorter.

---

## Anti-checklist — do not do these

- ❌ Do not add a second plant, a second pump, or a chatbot after hour 16.
- ❌ Do not calibrate the soil probe **after** it is in the pot.
- ❌ Do not test the pump on a table **above** the electronics.
- ❌ Do not put 12 V on a breadboard, or feed 12 V to a 3–6 V pump.
- ❌ Do not "quickly" swap a sensor 10 minutes before judging.
- ❌ Do not demo on venue Wi-Fi if your phone hotspot tested fine.
- ❌ Do not leave the pump dripping into the electronics while you present.
