# Alerts & rules

All rules live in `api/app.py` and run every 5 s, **per node**. Alert codes are
prefixed with the node name (`plant-a:soil_dry`) so two plants never share state.

## Rules

| Code | Level | Trigger | Clears when |
|---|---|---|---|
| `node_silent` | critical | no telemetry for `alert_silent_min` (default 5 min) | telemetry is < 90 s old again |
| `soil_dry` | warning | moisture below `alert_dry_pct` for `alert_dry_min` | moisture rises above the threshold |
| `tank_empty` | warning | tank below `alert_tank_pct` | tank rises above it |
| `sensor_fault` | warning | node reports a sensor in `fault` | node reports `fault: "ok"` |

The Pi raises these because a **dead sensor cannot report itself**. The node's
buzzer only covers things the node can still see (button press, NOT-AUS).

## Watering decision (comfort, not safety)

```
if pump_auto
   and not halted
   and moisture is not None and moisture < pump_threshold_pct
   and not on_cooldown           # pump_cooldown_min since last dose
   and pump_count_today < pump_max_per_day
   and tank is None or tank > alert_tank_pct
   and pump != "watering":
       publish planter/<node>/cmd {"action":"pump","seconds":pump_seconds}
```

Guards against over-watering: cooldown, daily cap, tank check, `mode:single` on
the node, and the firmware `pump_max_seconds` ceiling. **The node is the last
line of defence** — see [../PREFLIGHT.md](../PREFLIGHT.md) §5.

## Tuning

Settings are editable at runtime (dashboard **Einstellungen**, or
`PUT /api/config`). Defaults and env vars:

| Setting | Default | Meaning |
|---|---|---|
| `pump_auto` | true | enable auto watering |
| `pump_threshold_pct` | 30 | water below this soil moisture |
| `pump_seconds` | 20 | dose length (clamped ≤45 s by firmware) |
| `pump_cooldown_min` | 25 | minimum gap between doses |
| `pump_max_per_day` | 8 | hard daily cap |
| `alert_dry_pct` / `alert_dry_min` | 25 / 20 | dry-soil alert |
| `alert_silent_min` | 5 | node-silence alert |
| `alert_tank_pct` | 15 | tank-low alert |
| `light_auto` / `light_on_below_lux` | false / 4000 | grow-light automation |
| `buzzer_enabled` | true | allow buzzer commands |
| `telegram_enabled` | true | master switch for Telegram |

Some keys can also be seeded from `.env` (`ALERT_DRY_PCT`, `PUMP_SECONDS`, …).

## Telegram

Alerts are sent to the bot chat configured by `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID`. Each code has a **600 s anti-spam window**, so a second
identical alert inside 10 minutes is silently suppressed (expected, not a bug).

## Audit

Every raise/clear writes an `audit` row (`alert_raised` / `alert_cleared`) and an
`events` row (which drives the dashboard's Meldungen panel). The audit trail also
records logins, config changes and manual commands with actor + IP.
