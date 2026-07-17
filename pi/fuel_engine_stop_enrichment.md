# `engine.stop` payload enrichment (fuel dashboard)

Status: **approved, not yet applied.** Apply on the Pi at the next visit, then
redeploy the engine listener.

## Why

The app's sensorless fuel model (`src/lib/fuelModel.ts`) gets a bonus, high-
quality calibration anchor whenever the tank runs dry: an engine stop that
happened because the generator *starved*, not because the supervisor decided
the battery was full. A dry stop means level ≈ 0 at that instant, which lets
the model solve `r = levelBeforeLastFill / runtimeSinceLastFill` directly.

To tell a dry stop apart from a normal supervisor stop, the model needs two
extra fields on the `engine.stop` event payload. Today the Pi emits only
`{"state": state}` (see `engine.py` → `_emit_engine_edge_event`), so the dry-
stop path in the model simply never fires. The app already degrades
gracefully without this — enrichment only *adds* calibration accuracy.

## What to add

Enrich the `engine.stop` event payload with:

| field          | type              | meaning                                                        |
|----------------|-------------------|----------------------------------------------------------------|
| `reason`       | string enum       | why the engine stopped (see below)                             |
| `voltageAtStop`| number (volts)    | pack voltage at the moment of stop                             |
| `socAtStop`    | number \| null    | best SoC estimate at stop (LCD if awake, else VESC-volts, else null) |

`reason` enum (matches `EngineStopReason` in `src/firebase/types.ts`):

- `supervisor_full` — normal auto-stop; battery reached the stop threshold.
- `manual` — user pressed Stop.
- `stalled` — engine died on its own (RPM/current collapsed while spark
  still commanded). **This is the dry-stop signal.**
- `fault` — aborted by a fault (overtemp, low-voltage abort, error).

## The model's dry-stop rule (so the Pi side stays honest)

In `computeFuelModel`, a stop counts as "ran dry" only when:

```
reason === 'stalled'  AND  NOT (socAtStop >= 95)
```

i.e. the engine stalled while the battery was *not* essentially full. A stall
at high SoC is treated as a benign coincidence, not an empty tank. The model
further rejects implausible dry stops (a fault that killed the engine early
burned far less than a tank) via a runtime plausibility band, so an occasional
mislabel is self-correcting — but getting `reason` right keeps calibration
clean.

## Where to wire it in `engine.py`

`_emit_engine_edge_event(db, unit_id, state)` fires on the running→not-running
edge and currently writes `payload={"state": state}`. It needs the stop
`reason` threaded in from the caller that actually performed the stop:

- The stop paths live in `handle_engine_stop` (manual/graceful) and the
  supervisor's `_auto_stop` (`engine_supervisor.py`). Each knows its own
  reason.
- The `stalled` case is detected in the run/charge monitor loop — when RPM or
  motor current collapses while spark is still commanded, classify the ensuing
  stop as `stalled` rather than letting it look like a plain edge.

Suggested shape: pass an optional `reason` + a telemetry snapshot down to
`_emit_engine_edge_event` (or set a short-lived "pending stop reason" on the
edge tracker that the edge emitter consumes), then:

```python
payload = {"state": state}
if not running:
    payload["reason"] = reason or "manual"
    payload["voltageAtStop"] = last_pack_volts        # from VESC snapshot
    payload["socAtStop"] = last_soc_estimate          # LCD → VESC → None
db.collection(f"units/{unit_id}/events").add({
    "kind": kind,
    "at": firestore.SERVER_TIMESTAMP,
    "source": "pi",
    "payload": payload,
})
```

Keep it best-effort: if voltage/SoC aren't available at stop, omit them (or set
`socAtStop: null`) rather than blocking the event. The model treats a missing
`reason` as a non-dry stop, which is the safe default.

## Verify after deploy

1. Trigger a normal supervisor stop → event payload has
   `reason: "supervisor_full"`, high `socAtStop`. Model does **not** mark dry.
2. Let the tank run dry on a bench test (or simulate a stall) → payload has
   `reason: "stalled"`, low `socAtStop`. Model logs a dry-stop calibration
   sample and `ranDryDetected` flips true until the next refuel.
