# Fleet MCP server (AI fleet bot)

`functions/src/fleetMcp.ts` is a remote MCP server that lets an AI agent (a Grok
bot via the xAI API) monitor and operate every SitePulse unit. It writes the same
`units/{id}/commands` docs the app writes (`issuedBy: "bot:grok"`) and waits for
the Pi's ack, so all hardware safety stays on the Pi.

- **URL:** `https://us-central1-sitepulse-userapp.cloudfunctions.net/fleetMcp`
- **Transport:** MCP Streamable HTTP (stateless, JSON responses)
- **Auth:** the token in `~/.sitepulse-fleet-mcp-token`, also stored as the Firebase
  secret `FLEET_MCP_TOKEN`. Sent as `Authorization: Bearer <token>`; a bare token
  is also accepted.

## Tools

| Read | Control (need `reason`, optional `waitSec` 0–90, default 30) |
|---|---|
| `list_units`, `get_unit_status`, `get_events`, `get_config`, `get_command_result` | `start_engine`, `charge_engine`, `tune_charge`, `stop_engine`, `override_schedule`, `update_charge_schedule`, `toggle_ac`, `wake_lcd`, `set_fan`, `arm_sentry`, `disarm_sentry`, `camera_stream` |

These are intentionally left out: `engine.crank` and `servo.*` (bench-only), and
`light.set` / `relay.set`, because relay ch1 is the enclosure fan on UNIT-001.

## Safety policy: Firestore `fleet/bot`

```json
{
  "enabled": true,              // kill switch: false blocks every control tool, reads still work
  "defaultAccess": "full",      // 'full' | 'read' | 'off' for units not listed below
  "units": { "UNIT-002": "read" },
  "maxEngineStartsPerHour": 3,  // engine.start + engine.charge per unit, rolling hour
  "staleAfterSec": 180          // refuse control when telemetry is older (stop_engine exempt)
}
```

If the doc is missing, the bot is disabled. Edit it in the Firebase console. Changes
apply on the next tool call, with no redeploy.

## Audit and alerts

- Every control call and every refusal goes to the `fleetBotAudit` collection
  (tool, unit, args, reason, outcome, commandId).
- Engine, AC, sentry-disarm and schedule changes also write a `bot.command` event
  to the unit. That triggers the usual push/Telegram alert ("Fleet bot action:
  start_engine: <reason>") and shows as **FLEET BOT** in the app's Activity log.

## Connecting Grok (xAI Responses API)

```bash
curl https://api.x.ai/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -d '{
    "model": "grok-4.7",
    "input": [{"role": "user", "content": "Check the fleet. Any unit under 20% that is not charging?"}],
    "tools": [{
      "type": "mcp",
      "server_url": "https://us-central1-sitepulse-userapp.cloudfunctions.net/fleetMcp",
      "server_label": "sitepulse_fleet",
      "authorization": "Bearer '"$(cat ~/.sitepulse-fleet-mcp-token)"'"
    }]
  }'
```

To make a bot that can only watch, add
`"allowed_tools": ["list_units","get_unit_status","get_events","get_config","get_command_result"]`.

## Testing

`node scripts/fleet-mcp-smoke.mjs` connects as an MCP client, lists the tools, runs
read-only calls, and makes one call that is designed to be refused. It never moves hardware.

## Rotating the token

```bash
T="spfleet_$(openssl rand -hex 32)"; printf '%s\n' "$T" > ~/.sitepulse-fleet-mcp-token
printf '%s' "$T" | firebase functions:secrets:set FLEET_MCP_TOKEN --data-file=- --force
firebase deploy --only functions:fleetMcp
```

Then update the bot's config with the new token.

## Grok alerts (`functions/src/grokAlerts.ts`)

The cloud watches every unit and **wakes Grok directly** through the xAI API,
with this MCP server attached, when one of these fires:

| Alert | Fires when | Re-arms when |
|---|---|---|
| `battery_low` | Display SoC ≤ 10 % for 2 min. With the display asleep: pack ≤ 3.10 V/cell (46.5 V on 15S) at rest, not charging. | SoC ≥ 15 % or ≥ 3.20 V/cell |
| `pi_hot` | Pi SoC temp ≥ its warn threshold (75 °C) for 2 min, or it is throttling right now | 5 °C below the threshold |
| `engine_bogged` | 2nd `failed_engine_bogged` within 6 h | after firing, the count restarts |
| `offline` | No telemetry for 30 min (checked every 5 min) | Telemetry resumes, which also sends a "back online" Telegram |

Each alert fires **once per incident**. State is kept in `units/{id}/alerts/state`.
`battery_low` and `offline` also write a unit event (`battery.low`,
`system.offline`), so the owner's push, the unit Telegram and the Activity log
show them. Grok's report goes **only** to `fleet/bot.escalationTelegramChatIds`,
never to the unit's own chats, which a customer may share. Every wake-up is
logged in `fleetBotWakes` with the report, outcome and duration. Grok gets its
last 3 reports for that unit as context.

Settings in `fleet/bot`: `alertsEnabled`, `escalationTelegramChatIds`, `grokModel`
(default `grok-4.7`), `maxWakesPerDay` (default 24; past the cap you get a
Telegram saying so instead), `lowSocPct` / `clearSocPct` (10 / 15), and
`offlineAfterMin` (30). Secrets: `XAI_API_KEY`, `FLEET_MCP_TOKEN`,
`TELEGRAM_BOT_TOKEN`.

The system prompt Grok gets is in `functions/src/grokPrompt.ts`. It mirrors
section 9 of the handoff doc, so change both together.
