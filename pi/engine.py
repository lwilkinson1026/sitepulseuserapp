"""
Engine lifecycle orchestrator (phase G.2 + G.3 + G.4).

Four public command handlers:

  handle_engine_crank  — low-level: crank only. No choke or spark management.
  handle_engine_start  — full start choreography: choke → spark → crank.
  handle_engine_charge — apply regen load to a running engine to charge the
                         pack until voltage threshold. Runs as a background
                         thread; returns immediately after spawning.
  handle_engine_stop   — graceful shutdown: abort charge → spark off → wait
                         for RPM to fall to idle → publish "idle".

start, crank, and stop share `_engine_lock` (only one at a time). charge
spawns a daemon thread (`_charge_thread`) so it doesn't hold the lock —
the long-running loop is monitored via `_charge_abort` (signal stop) and
`_charge_active` (is it running?). start/crank refuse to run while
_charge_active is set; stop is the only thing that can interrupt it.

The crank primitive
-------------------
Drive the VESC at a commanded motor current for up to maxDurationSec,
refreshing the command at refreshHz to keep the VESC's command-timeout
watchdog satisfied. Aborts when the engine catches OR when the duration
ceiling is hit.

Catch detection (no tach required)
----------------------------------
We detect engine catch via the VESC's own motor-current telemetry, which we
read from STATUS_1 frames as they fly by on the bus during the crank.

  - During cranking, motor current sits at the commanded value (~currentAmps).
  - When the engine fires, it produces its own torque and overruns the
    starter. The motor current crashes — often into regen (negative).
  - If |motor_amps| stays below `currentAmps × catchCurrentRatio` for
    `catchHoldMs` consecutive milliseconds, the engine has caught.
  - Strong regen (motor_amps < -10 A) short-circuits to "caught immediately"
    since that's an unambiguous engine-overrunning-starter signal.

To avoid false positives when there's no motor (the controller can't
deliver any current and motor_amps reads 0 the whole time), catch detection
is gated by an "armed" flag — we only consider catch *after* we've seen
the motor actually pull at least 50% of the commanded current. If we never
arm during maxDurationSec, the result is "failed_no_load" — usually means
no motor wired up.

State machine (published to units/{unit_id}/current/engine)
-----------------------------------------------------------
  idle            → before any crank attempt
  cranking        → crank loop running
  running         → engine caught; loop released the motor
  failed_no_catch → max duration elapsed without a catch event
  failed_no_load  → never observed real current draw (likely no motor)
  failed_error    → exception during crank (logged in `lastError`)

Preconditions NOT enforced here (the caller is responsible):
  - Spark relay is on
  - Choke is in cranking position
  - VESC is online and STATUS frames are flowing

This is a *pure crank* primitive. A higher-level engine.start macro that
orchestrates choke + spark + crank + transition lives in a future phase.
"""

from __future__ import annotations

import os
import struct
import threading
import time
from typing import Any, Dict, Optional

from firebase_admin import firestore

from vesc_listener import CMD_STATUS_1, CMD_STATUS_4, CMD_STATUS_5
from vesc_sender import VescSender


# Hard safety ceilings (cannot be exceeded by config or payload override)
MAX_CURRENT_AMPS_HARD     = 150.0
MAX_DURATION_SEC_HARD     = 8.0
MIN_DURATION_SEC_HARD     = 0.5
MAX_REFRESH_HZ            = 50
MIN_REFRESH_HZ            = 5

# Unambiguous-catch threshold: any regen current below this immediately
# triggers "caught" without waiting for the hold window. Negative because
# regen = current flowing back into the pack.
STRONG_REGEN_AMPS         = -10.0

# Below this fraction of commanded current we consider the motor "loose"
# (engine has caught OR there's no motor).
DEFAULT_CATCH_CURRENT_RATIO = 0.25

# Defaults baked in here so a missing config doc doesn't brick cranking.
# These mirror seed_config.py's `engine.cranking` block.
DEFAULT_CRANK_CONFIG: Dict[str, Any] = {
    "currentAmps": 65.0,
    "maxDurationSec": 4.0,
    "refreshHz": 10,
    "catchCurrentRatio": DEFAULT_CATCH_CURRENT_RATIO,
    "catchHoldMs": 200,
    # The motor must pull ≥ this fraction of commanded current at least once
    # for the catch-detection loop to "arm". Filters dry-runs without a motor.
    "armCurrentRatio": 0.5,
}


# Env
VESC_IFACE   = os.environ.get("SITEPULSE_VESC_IFACE", "can0")
VESC_UNIT_ID = int(os.environ.get("SITEPULSE_VESC_UNIT_ID", "0"))

# Module-level mutex: only one synchronous engine action (start/crank/stop)
# runs at a time. The charge loop runs in a background thread WITHOUT
# holding this lock — it's gated separately by `_charge_active`.
_engine_lock = threading.Lock()

# Charge thread state
_charge_thread: Optional[threading.Thread] = None
_charge_abort  = threading.Event()   # set to request the charge loop exit
_charge_active = threading.Event()   # set while charge thread is running

# Live-tune state. The charge loop reads `_charge_target_amps` every send tick
# instead of using a value captured at start, so engine.charge.tune commands
# can adjust the target while the loop is running. Slewed toward via
# MAX_SLEW_AMPS_PER_SEC so a 5A→30A jump can't whip the motor in one tick.
_charge_target_lock        = threading.Lock()
_charge_target_amps: Optional[float] = None    # live target (None when not charging)
_charge_applied_amps        = 0.0              # what the loop is currently sending
MAX_SLEW_AMPS_PER_SEC      = 10.0              # hard ramp rate, both directions


# Defaults for the start macro. Mirror seed_config.py's `engine.start` block.
DEFAULT_START_CONFIG: Dict[str, Any] = {
    "chokePreset":             "start_cold",
    "runChokePreset":          "run",         # after engine catches
    "failChokePreset":         "idle",        # on crank failure
    "sparkRelayChannel":       2,             # "Aux 2" per the deployed wiring
    "chokeSettleMs":           500,           # pause between choke set and spark on
    "sparkSettleMs":           200,           # pause between spark on and crank
    "postCatchSettleMs":       2000,          # pause between catch and run-choke
    "turnOffSparkOnFailure":   True,
}


# Defaults for the charge loop (phase G.4). Mirror config/engine.charge.
DEFAULT_CHARGE_CONFIG: Dict[str, Any] = {
    # Amps to extract from the motor (generator mode). Sent as negative
    # SET_CURRENT. Conservative default so the engine isn't bogged down
    # before we know what it can sustain.
    "currentAmps":      25.0,
    # Voltage thresholds. Pack voltage as reported by the VESC.
    # 14S LiFePO4: ~49.5 V resting full (measured on UNIT-002 2026-07-29),
    # ~48.1 V ≈ 90 % SOC, ~43.4 V ≈ 10 % SOC. BMS termination is around
    # 3.5 V/cell × 14 = 49.0 V. Derive these with voltage_soc.py, don't
    # retype them — a pack voltage means nothing without its cell count.
    "voltageStop":      49.0,    # exit charge loop when pack rises above this
    # Hard pack-protection floor: abort charge if pack sags below this.
    # NOT meant to detect "engine not generating" (the minRpmForLoad bog
    # check already does that) — a big external load legitimately sags the
    # pack well below resting, and aborting only drains it faster. So this
    # sits near BMS-cutoff territory: 42.0 V = 3.00 V/cell on 14S LiFePO4,
    # ~7 V above the ~35 V BMS cut, comfortably below heavy-load sag.
    "voltageMinAbort":  42.0,
    # Safety ceiling — even with no other exit, charge dies after this much
    # wall-clock time, then the engine auto-stops (see _run_charge_loop's
    # autostop_on_timeout). Defaults to the 2-hour hard max. Code clamps to
    # MAX_CHARGE_DURATION_HARD regardless.
    "maxDurationSec":   7200,
    # Watchdog refresh rate for SET_CURRENT.
    "refreshHz":        10,
    # Engine RPM minimum. If RPM drops below this, the engine is being
    # bogged down — abort to avoid stalling.
    "minRpmForLoad":    800,
    # Temperature safety. Abort if either exceeds.
    "maxFetTempC":      80.0,
    "maxMotorTempC":    100.0,
    # Soft-start window. The charge loop slews 0→currentAmps over this many
    # seconds (currentAmps/rampUpSec A/s), so the engine takes load on
    # gradually rather than all at once. Also the settle window before
    # low-voltage / bog-down safety checks arm.
    "rampUpSec":        60.0,
}


# Defaults for the stop primitive.
DEFAULT_STOP_CONFIG: Dict[str, Any] = {
    "rpmIdleThreshold":  100,    # motor_rpm considered "stopped"
    "rpmWaitTimeoutSec": 15,     # max time we'll wait for engine to coast down
    "sparkRelayChannel": 2,      # mirror engine.start.sparkRelayChannel
    "chargeJoinTimeoutSec": 5,   # how long stop waits for the charge thread to exit
}


# Charge hard ceilings (cannot be exceeded by config)
MAX_CHARGE_AMPS_HARD      = 50.0
MAX_CHARGE_DURATION_HARD  = 7200.0   # 2 hours absolute


# ─── config + state helpers ────────────────────────────────────────────────

def _load_crank_config(db: firestore.Client, unit_id: str) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CRANK_CONFIG)
    try:
        snap = db.document(f"units/{unit_id}/config/engine").get()
        if snap.exists:
            block = (snap.to_dict() or {}).get("cranking", {})
            cfg.update(block)
    except Exception as e:
        print(f"[engine] config/engine read failed, using defaults: {e}", flush=True)
    return cfg


def _load_start_config(db: firestore.Client, unit_id: str) -> Dict[str, Any]:
    cfg = dict(DEFAULT_START_CONFIG)
    try:
        snap = db.document(f"units/{unit_id}/config/engine").get()
        if snap.exists:
            block = (snap.to_dict() or {}).get("start", {})
            cfg.update(block)
    except Exception as e:
        print(f"[engine] config/engine.start read failed, using defaults: {e}", flush=True)
    return cfg


def _load_charge_config(db: firestore.Client, unit_id: str) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CHARGE_CONFIG)
    try:
        snap = db.document(f"units/{unit_id}/config/engine").get()
        if snap.exists:
            block = (snap.to_dict() or {}).get("charge", {})
            cfg.update(block)
    except Exception as e:
        print(f"[engine] config/engine.charge read failed, using defaults: {e}", flush=True)
    return cfg


def _load_stop_config(db: firestore.Client, unit_id: str) -> Dict[str, Any]:
    cfg = dict(DEFAULT_STOP_CONFIG)
    try:
        snap = db.document(f"units/{unit_id}/config/engine").get()
        if snap.exists:
            block = (snap.to_dict() or {}).get("stop", {})
            cfg.update(block)
    except Exception as e:
        print(f"[engine] config/engine.stop read failed, using defaults: {e}", flush=True)
    return cfg


# States in which the engine is physically turning. Crossing into/out of this
# set is what we notify on (engine.start / engine.stop events).
_ENGINE_RUNNING_STATES = ("running", "charging")

# Edge tracker for engine.start/stop event emission. _publish_state is the
# single chokepoint every transition passes through (manual, supervisor, and
# scare paths all funnel here), so tracking the running/not-running edge here
# gives exactly one event per real start and one per real shutdown, regardless
# of who triggered it. Process-local: reset to a clean 'idle' on listener boot
# via init_engine().
_engine_event_lock = threading.Lock()
_last_engine_running: Dict[str, bool] = {}


def _emit_engine_edge_event(db: firestore.Client, unit_id: str, state: str) -> None:
    """Emit a canonical engine.start / engine.stop event when the engine
    crosses into or out of a running state. Writes the app/Function schema
    (`kind`/`at`/`source`/`payload`) so it shows in the Activity feed AND
    triggers the push/Telegram fan-out. Best-effort — never blocks state
    publishing."""
    running = state in _ENGINE_RUNNING_STATES
    with _engine_event_lock:
        prev = _last_engine_running.get(unit_id)
        if prev == running:
            return
        _last_engine_running[unit_id] = running
        # First observation for this unit (typically the boot 'idle'): record
        # the baseline but don't emit a spurious 'stop' for a not-running seed.
        if prev is None and not running:
            return
        kind = "engine.start" if running else "engine.stop"
    try:
        db.collection(f"units/{unit_id}/events").add({
            "kind": kind,
            "at": firestore.SERVER_TIMESTAMP,
            "source": "pi",
            "payload": {"state": state},
        })
    except Exception as e:
        print(f"[engine] event emit failed for {kind}: {e}", flush=True)


def _publish_state(
    db: firestore.Client,
    unit_id: str,
    state: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    payload: Dict[str, Any] = {
        "state": state,
        "lastChangedAt": firestore.SERVER_TIMESTAMP,
    }
    if extra:
        payload.update(extra)
    try:
        db.document(f"units/{unit_id}/current/engine").set(payload, merge=True)
    except Exception as e:
        print(f"[engine] failed to publish state {state!r}: {e}", flush=True)

    # Fire engine.start / engine.stop on the running-state edge (best-effort).
    _emit_engine_edge_event(db, unit_id, state)

    # Drive any engine-follow aux relay (e.g. cooling fans on a channel set to
    # 'auto') to match. Lazy import keeps engine.py importable off-Pi; the
    # reconcile is best-effort so a GPIO hiccup never blocks state publishing.
    try:
        from relays import reconcile_engine_follow
        reconcile_engine_follow(db, unit_id, state)
    except Exception as e:
        print(f"[engine] aux-follow reconcile skipped: {e}", flush=True)


def init_engine(db: firestore.Client, unit_id: str) -> None:
    """Publish 'idle' on listener startup so the app sees a clean state.
    Called once at boot from command_listener's background-services list."""
    _publish_state(db, unit_id, "idle", {"bootedAt": firestore.SERVER_TIMESTAMP})


# ─── crank loop ────────────────────────────────────────────────────────────

class _CrankResult:
    __slots__ = ("state", "metadata")

    def __init__(self, state: str, metadata: Dict[str, Any]) -> None:
        self.state = state
        self.metadata = metadata


def _run_crank_loop(
    sender: VescSender,
    bus: Any,
    current_amps: float,
    max_duration_sec: float,
    refresh_hz: int,
    catch_current_ratio: float,
    catch_hold_ms: int,
    arm_current_ratio: float,
) -> _CrankResult:
    import can  # type: ignore[import-not-found]  # noqa: F401  (for typing only)

    start              = time.monotonic()
    last_send_at       = 0.0
    refresh_interval_s = 1.0 / refresh_hz
    catch_hold_s       = catch_hold_ms / 1000.0
    catch_threshold    = current_amps * catch_current_ratio
    arm_threshold      = current_amps * arm_current_ratio

    armed                 = False
    last_low_current_at   = None
    peak_observed_amps    = 0.0
    last_observed_amps    = 0.0

    status1_arb_id = (CMD_STATUS_1 << 8) | (sender.unit_id & 0xFF)

    while True:
        now     = time.monotonic()
        elapsed = now - start

        if elapsed >= max_duration_sec:
            sender.release()
            if not armed:
                return _CrankResult("failed_no_load", {
                    "durationSec": round(elapsed, 3),
                    "peakCurrentAmps": round(peak_observed_amps, 2),
                    "reason": "motor current never reached arm threshold",
                })
            return _CrankResult("failed_no_catch", {
                "durationSec": round(elapsed, 3),
                "peakCurrentAmps": round(peak_observed_amps, 2),
            })

        # Refresh the SET_CURRENT command at refresh_hz cadence to keep
        # the VESC's watchdog satisfied.
        if now - last_send_at >= refresh_interval_s:
            sender.set_current(current_amps)
            last_send_at = now

        # Read any pending frame with a tight timeout so the send-refresh
        # cadence stays close to refresh_hz.
        msg = bus.recv(timeout=0.01)
        if msg is None:
            continue
        if not msg.is_extended_id:
            continue
        if msg.arbitration_id != status1_arb_id:
            continue
        if len(msg.data) < 8:
            continue

        # STATUS_1: bytes 4-5 are Total Current ×10, signed int16 BE.
        motor_amps = struct.unpack(">h", msg.data[4:6])[0] / 10.0
        last_observed_amps = motor_amps
        if abs(motor_amps) > peak_observed_amps:
            peak_observed_amps = abs(motor_amps)

        # Arm catch detection only after observing real current draw.
        if not armed and abs(motor_amps) >= arm_threshold:
            armed = True

        if not armed:
            continue

        # Strong regen → engine definitely caught and back-driving the starter.
        if motor_amps < STRONG_REGEN_AMPS:
            sender.release()
            return _CrankResult("running", {
                "durationSec": round(time.monotonic() - start, 3),
                "peakCurrentAmps": round(peak_observed_amps, 2),
                "catchSignal": "strong_regen",
                "lastMotorAmps": round(motor_amps, 2),
            })

        # Sustained low current → engine probably caught.
        if abs(motor_amps) < catch_threshold:
            if last_low_current_at is None:
                last_low_current_at = now
            elif now - last_low_current_at >= catch_hold_s:
                sender.release()
                return _CrankResult("running", {
                    "durationSec": round(time.monotonic() - start, 3),
                    "peakCurrentAmps": round(peak_observed_amps, 2),
                    "catchSignal": "low_current_hold",
                    "lastMotorAmps": round(motor_amps, 2),
                })
        else:
            last_low_current_at = None


# ─── internal: pure crank, no lock, no state publishing ───────────────────

def _resolve_crank_params(
    cfg: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Combine config + payload overrides, clamp to hard safety ceilings.
    Centralized so engine.crank and engine.start can't drift."""
    current_amps = float(payload.get("currentAmpsOverride", cfg.get("currentAmps", 65)))
    max_dur      = float(payload.get("maxDurationSecOverride", cfg.get("maxDurationSec", 4)))
    refresh_hz   = int(cfg.get("refreshHz", 10))
    catch_ratio  = float(cfg.get("catchCurrentRatio", DEFAULT_CATCH_CURRENT_RATIO))
    catch_hold_ms = int(cfg.get("catchHoldMs", 200))
    arm_ratio    = float(cfg.get("armCurrentRatio", 0.5))

    return {
        "current_amps": max(0.0, min(MAX_CURRENT_AMPS_HARD, current_amps)),
        "max_duration_sec": max(MIN_DURATION_SEC_HARD, min(MAX_DURATION_SEC_HARD, max_dur)),
        "refresh_hz": max(MIN_REFRESH_HZ, min(MAX_REFRESH_HZ, refresh_hz)),
        "catch_current_ratio": catch_ratio,
        "catch_hold_ms": catch_hold_ms,
        "arm_current_ratio": arm_ratio,
    }


def _do_crank(params: Dict[str, Any]) -> _CrankResult:
    """Open the bus, run the crank loop, close the bus. Returns the result.

    Caller responsibilities:
      - Hold `_engine_lock`.
      - Publish state transitions to current/engine.

    This function is the shared primitive used by both `handle_engine_crank`
    and the start-macro's `_do_start`.
    """
    sender = VescSender(iface=VESC_IFACE, unit_id=VESC_UNIT_ID)
    sender.open()
    try:
        bus = sender._bus  # noqa: SLF001 (intentional same-bus read+write)
        return _run_crank_loop(
            sender, bus,
            current_amps=params["current_amps"],
            max_duration_sec=params["max_duration_sec"],
            refresh_hz=params["refresh_hz"],
            catch_current_ratio=params["catch_current_ratio"],
            catch_hold_ms=params["catch_hold_ms"],
            arm_current_ratio=params["arm_current_ratio"],
        )
    finally:
        # Even if the loop raised, send one final release before closing.
        try:
            sender.release()
        except Exception:
            pass
        sender.close()


# ─── public command handler: crank only ────────────────────────────────────

def handle_engine_crank(
    db: firestore.Client,
    unit_id: str,
    payload: Dict[str, Any],
) -> None:
    """Run one crank attempt. No choke or spark management — caller is
    responsible for those if they want a runnable engine.

    payload (all optional):
      currentAmpsOverride   — override config/engine.cranking.currentAmps
      maxDurationSecOverride — override config/engine.cranking.maxDurationSec
    """
    if not _engine_lock.acquire(blocking=False):
        raise RuntimeError("engine.crank: already in progress")

    try:
        if _charge_active.is_set():
            raise RuntimeError(
                "engine.crank: charge loop is active; engine.stop first"
            )
        cfg    = _load_crank_config(db, unit_id)
        params = _resolve_crank_params(cfg, payload)

        _publish_state(db, unit_id, "cranking", {
            "startedAt":            firestore.SERVER_TIMESTAMP,
            "currentAmpsCommanded": params["current_amps"],
            "maxDurationSec":       params["max_duration_sec"],
            "refreshHz":            params["refresh_hz"],
            "trigger":              "manual_crank",
        })

        try:
            result = _do_crank(params)
        except Exception as e:
            _publish_state(db, unit_id, "failed_error", {"error": str(e)})
            print(f"[engine] crank failed with exception: {e!r}", flush=True)
            raise

        _publish_state(db, unit_id, result.state, result.metadata)
        print(
            f"[engine] crank done: state={result.state!r} meta={result.metadata}",
            flush=True,
        )
    finally:
        _engine_lock.release()


# ─── public command handler: full start choreography ──────────────────────

def _set_choke_safely(db: firestore.Client, unit_id: str, preset: str) -> None:
    """Set the choke via the servo preset handler. Imported lazily so this
    module stays importable on a Mac (no hardware deps)."""
    from servos import handle_servo_preset
    handle_servo_preset(db, unit_id, {"name": preset})


def _set_spark_safely(
    db: firestore.Client, unit_id: str, channel: int, mode: str,
) -> None:
    """Drive the spark relay via the existing relay handler. Lazy import for
    the same reason as the servo helper."""
    from relays import handle_relay_set
    handle_relay_set(db, unit_id, {"channel": channel, "mode": mode})


def handle_engine_start(
    db: firestore.Client,
    unit_id: str,
    payload: Dict[str, Any],
) -> None:
    """Full engine start choreography: choke → spark → crank → (on catch)
    open choke, (on failure) reset choke + spark off.

    payload (all optional):
      chokePreset            — override start.chokePreset for this attempt
      currentAmpsOverride    — passed through to the crank step
      maxDurationSecOverride — passed through to the crank step
    """
    if not _engine_lock.acquire(blocking=False):
        raise RuntimeError("engine.start: already in progress")

    try:
        if _charge_active.is_set():
            raise RuntimeError(
                "engine.start: charge loop is active; engine.stop first"
            )
        start_cfg     = _load_start_config(db, unit_id)
        crank_cfg     = _load_crank_config(db, unit_id)
        crank_params  = _resolve_crank_params(crank_cfg, payload)

        choke_preset      = payload.get("chokePreset", start_cfg["chokePreset"])
        run_choke_preset  = start_cfg["runChokePreset"]
        fail_choke_preset = start_cfg["failChokePreset"]
        spark_channel     = int(start_cfg["sparkRelayChannel"])
        choke_settle_s    = max(0.0, float(start_cfg["chokeSettleMs"])) / 1000.0
        spark_settle_s    = max(0.0, float(start_cfg["sparkSettleMs"])) / 1000.0
        post_catch_s      = max(0.0, float(start_cfg["postCatchSettleMs"])) / 1000.0
        turn_off_spark    = bool(start_cfg["turnOffSparkOnFailure"])

        sequence_started_at = time.monotonic()

        # 1. Choke to cranking position
        _publish_state(db, unit_id, "starting", {
            "startedAt":            firestore.SERVER_TIMESTAMP,
            "phase":                "choke_setting",
            "chokePreset":          choke_preset,
            "currentAmpsCommanded": crank_params["current_amps"],
        })
        _set_choke_safely(db, unit_id, choke_preset)
        time.sleep(choke_settle_s)

        # 2. Spark relay on
        _publish_state(db, unit_id, "starting", {"phase": "spark_on"})
        _set_spark_safely(db, unit_id, spark_channel, "on")
        time.sleep(spark_settle_s)

        # 3. Crank
        _publish_state(db, unit_id, "cranking", {
            "currentAmpsCommanded": crank_params["current_amps"],
            "maxDurationSec":       crank_params["max_duration_sec"],
            "refreshHz":            crank_params["refresh_hz"],
            "trigger":              "engine_start",
        })

        try:
            result = _do_crank(crank_params)
        except Exception as e:
            # Crank raised — best-effort cleanup before bubbling up.
            print(f"[engine.start] crank raised: {e!r}; resetting choke + spark", flush=True)
            try:
                _set_choke_safely(db, unit_id, fail_choke_preset)
            except Exception as ce:
                print(f"[engine.start] choke reset failed: {ce!r}", flush=True)
            if turn_off_spark:
                try:
                    _set_spark_safely(db, unit_id, spark_channel, "off")
                except Exception as se:
                    print(f"[engine.start] spark off failed: {se!r}", flush=True)
            _publish_state(db, unit_id, "failed_error", {
                "error": str(e), "phase": "cranking",
            })
            raise

        # 4. Post-crank
        sequence_total_s = round(time.monotonic() - sequence_started_at, 3)

        if result.state == "running":
            # Engine caught. Stabilize, then open the choke.
            _publish_state(db, unit_id, "running", {
                **result.metadata,
                "phase":           "stabilizing",
                "sequenceTotalSec": sequence_total_s,
            })
            time.sleep(post_catch_s)
            try:
                _set_choke_safely(db, unit_id, run_choke_preset)
            except Exception as e:
                print(f"[engine.start] run-choke set failed: {e!r}", flush=True)
            _publish_state(db, unit_id, "running", {
                **result.metadata,
                "phase":           "complete",
                "sequenceTotalSec": round(time.monotonic() - sequence_started_at, 3),
            })
            print(
                f"[engine.start] success: caught after {result.metadata.get('durationSec', '?')}s "
                f"(total seq {sequence_total_s}s)",
                flush=True,
            )
        else:
            # Crank failed (timeout / no load). Reset choke + spark, propagate state.
            try:
                _set_choke_safely(db, unit_id, fail_choke_preset)
            except Exception as e:
                print(f"[engine.start] choke reset failed: {e!r}", flush=True)
            if turn_off_spark:
                try:
                    _set_spark_safely(db, unit_id, spark_channel, "off")
                except Exception as e:
                    print(f"[engine.start] spark off failed: {e!r}", flush=True)
            _publish_state(db, unit_id, result.state, {
                **result.metadata,
                "sequenceTotalSec": sequence_total_s,
            })
            print(
                f"[engine.start] failed: state={result.state!r} meta={result.metadata}",
                flush=True,
            )

    finally:
        _engine_lock.release()


# ─── charge loop (background thread, phase G.4) ────────────────────────────

def _resolve_charge_params(
    cfg: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Combine config + payload overrides, clamp to hard ceilings."""
    current_amps = float(payload.get("currentAmpsOverride", cfg.get("currentAmps", 10)))
    voltage_stop = float(payload.get("voltageStopOverride", cfg.get("voltageStop", 49.0)))
    max_dur      = float(payload.get("maxDurationSecOverride", cfg.get("maxDurationSec", 3600)))

    return {
        "current_amps":      max(0.0, min(MAX_CHARGE_AMPS_HARD, current_amps)),
        "voltage_stop":      voltage_stop,
        "voltage_min_abort": float(cfg.get("voltageMinAbort", 42.0)),
        "max_duration_sec":  max(1.0, min(MAX_CHARGE_DURATION_HARD, max_dur)),
        "refresh_hz":        max(MIN_REFRESH_HZ, min(MAX_REFRESH_HZ, int(cfg.get("refreshHz", 10)))),
        "min_rpm_for_load":  int(cfg.get("minRpmForLoad", 800)),
        "max_fet_temp_c":    float(cfg.get("maxFetTempC", 80.0)),
        "max_motor_temp_c":  float(cfg.get("maxMotorTempC", 100.0)),
        "ramp_up_sec":       max(0.0, float(cfg.get("rampUpSec", 2.0))),
    }


def _run_charge_loop(
    db: firestore.Client,
    unit_id: str,
    params: Dict[str, Any],
) -> None:
    """Long-running charge loop. Sends SET_CURRENT(-applied_amps) at
    refresh_hz, slewing applied_amps toward the live shared target each
    send tick. Reads STATUS frames inline, exits on any of:
      - voltage rises above voltage_stop  → state="idle" (success)
      - voltage falls below voltage_min_abort → state="failed_low_voltage"
      - RPM falls below min_rpm_for_load   → state="failed_engine_bogged"
      - FET or motor temp exceeds limit    → state="failed_overtemp"
      - max_duration_sec exceeded          → state="failed_timeout"
      - _charge_abort signaled (engine.stop) → state="stopped"
    """
    global _charge_target_amps, _charge_applied_amps

    voltage_stop    = params["voltage_stop"]
    voltage_min     = params["voltage_min_abort"]
    max_dur         = params["max_duration_sec"]
    refresh_hz      = params["refresh_hz"]
    min_rpm         = params["min_rpm_for_load"]
    max_fet_temp    = params["max_fet_temp_c"]
    max_motor_temp  = params["max_motor_temp_c"]
    # ramp_up_sec drives a genuine soft-start: during the initial window we
    # slew at current_amps / ramp_up_sec (e.g. 25A over 60s ≈ 0.42 A/s) so the
    # engine takes on load gradually instead of being slammed to full regen.
    # Past the window we revert to MAX_SLEW_AMPS_PER_SEC so mid-run
    # engine.charge.tune adjustments stay responsive. ramp_up_sec also gates
    # the post-ramp safety checks (low_volts/min_rpm), so a long soft-start
    # window naturally suppresses aborts on the expected ramp-in transients.
    settle_sec      = params["ramp_up_sec"]
    # A/s rate used only while elapsed <= ramp_up_sec. Clamp to the hard slew
    # ceiling so a tiny ramp_up_sec can't out-run the motor-protection limit.
    if settle_sec > 0:
        soft_start_rate = min(MAX_SLEW_AMPS_PER_SEC, params["current_amps"] / settle_sec)
    else:
        soft_start_rate = MAX_SLEW_AMPS_PER_SEC

    refresh_interval = 1.0 / refresh_hz

    with _charge_target_lock:
        _charge_target_amps  = params["current_amps"]
        _charge_applied_amps = 0.0

    # Set when the charge ends on a duration timeout — we shut the engine
    # down after the loop unwinds (see end of function). Spawning the stop
    # only after _charge_active clears avoids handle_engine_stop trying to
    # join this very thread.
    autostop_on_timeout = False

    sender = VescSender(iface=VESC_IFACE, unit_id=VESC_UNIT_ID)
    try:
        sender.open()
        bus = sender._bus  # noqa: SLF001

        status1_id = (CMD_STATUS_1 << 8) | (sender.unit_id & 0xFF)
        status4_id = (CMD_STATUS_4 << 8) | (sender.unit_id & 0xFF)
        status5_id = (CMD_STATUS_5 << 8) | (sender.unit_id & 0xFF)

        started_at        = time.monotonic()
        last_send_at      = 0.0
        last_volts        = None
        last_rpm          = None
        last_fet_temp     = None
        last_motor_temp   = None
        peak_volts        = 0.0

        result_state    = None
        result_metadata: Dict[str, Any] = {}

        _publish_state(db, unit_id, "charging", {
            "startedAt":            firestore.SERVER_TIMESTAMP,
            "currentAmpsCommanded": params["current_amps"],
            "voltageStop":          voltage_stop,
            "maxDurationSec":       max_dur,
        })

        while not _charge_abort.is_set():
            now      = time.monotonic()
            elapsed  = now - started_at

            if elapsed >= max_dur:
                result_state    = "failed_timeout"
                result_metadata = {"durationSec": round(elapsed, 1)}
                autostop_on_timeout = True
                break

            # Send tick: slew applied_amps toward the live shared target. During
            # the soft-start window the per-step cap is soft_start_rate * dt;
            # afterward it's MAX_SLEW_AMPS_PER_SEC * dt.
            if now - last_send_at >= refresh_interval:
                dt = max(refresh_interval, now - last_send_at) if last_send_at > 0 else refresh_interval
                with _charge_target_lock:
                    target_now = _charge_target_amps or 0.0
                target_now = max(0.0, min(MAX_CHARGE_AMPS_HARD, target_now))

                slew_rate = soft_start_rate if elapsed <= settle_sec else MAX_SLEW_AMPS_PER_SEC
                max_delta = slew_rate * dt
                if abs(target_now - _charge_applied_amps) <= max_delta:
                    _charge_applied_amps = target_now
                else:
                    _charge_applied_amps += (
                        max_delta if target_now > _charge_applied_amps else -max_delta
                    )
                # Negative SET_CURRENT = regen / generator mode = charge pack.
                sender.set_current(-_charge_applied_amps)
                last_send_at = now

            msg = bus.recv(timeout=0.01)
            if msg is None:
                continue
            if not msg.is_extended_id or len(msg.data) < 8:
                continue

            arb = msg.arbitration_id
            if arb == status1_id:
                last_rpm = struct.unpack(">i", msg.data[0:4])[0]
            elif arb == status4_id:
                last_fet_temp   = struct.unpack(">h", msg.data[0:2])[0] / 10.0
                last_motor_temp = struct.unpack(">h", msg.data[2:4])[0] / 10.0
            elif arb == status5_id:
                last_volts = struct.unpack(">h", msg.data[4:6])[0] / 10.0
                if last_volts > peak_volts:
                    peak_volts = last_volts

            # Safety checks only meaningful past the initial settle window.
            # Voltage sag and RPM dip are expected during the 0→target slew,
            # so we don't act on them until the load has had a moment to
            # stabilize. Re-arm window only on initial start, not on each tune.
            if elapsed <= settle_sec:
                continue

            if last_volts is not None and last_volts >= voltage_stop:
                # Engine is still running with spark on — we just released
                # the motor. Caller (or supervisor) is responsible for
                # firing engine.stop to actually shut the engine down.
                result_state    = "running"
                result_metadata = {
                    "reason":       "voltage_stop_reached",
                    "durationSec":  round(elapsed, 1),
                    "peakVoltage":  round(peak_volts, 2),
                    "finalVoltage": round(last_volts, 2),
                    "phase":        "charge_complete",
                }
                break
            if last_volts is not None and last_volts <= voltage_min:
                result_state    = "failed_low_voltage"
                result_metadata = {
                    "finalVoltage": round(last_volts, 2),
                    "durationSec":  round(elapsed, 1),
                }
                break
            if last_rpm is not None and last_rpm < min_rpm:
                result_state    = "failed_engine_bogged"
                result_metadata = {
                    "finalRpm":    last_rpm,
                    "durationSec": round(elapsed, 1),
                }
                break
            if last_fet_temp is not None and last_fet_temp >= max_fet_temp:
                result_state    = "failed_overtemp"
                result_metadata = {
                    "fetTempC":    round(last_fet_temp, 1),
                    "durationSec": round(elapsed, 1),
                }
                break
            # Only respect motor temp if the reading looks plausible —
            # a disconnected probe reports ~388 °C and would always trip.
            if (
                last_motor_temp is not None
                and last_motor_temp >= max_motor_temp
                and last_motor_temp < 250.0
            ):
                result_state    = "failed_overtemp"
                result_metadata = {
                    "motorTempC":  round(last_motor_temp, 1),
                    "durationSec": round(elapsed, 1),
                }
                break

        try:
            sender.release()
        except Exception:
            pass

        # Abort signaled (engine.stop) — let stop's own state publishes
        # take over. The motor is released; engine.stop will publish
        # "stopping" → "idle" as it walks through its sequence.
        if _charge_abort.is_set() and result_state is None:
            print(
                f"[engine] charge aborted after {round(time.monotonic() - started_at, 1)}s "
                f"(peak {round(peak_volts, 2)} V) — engine.stop owns state from here",
                flush=True,
            )
            return  # explicitly skip the publish-state below

        _publish_state(db, unit_id, result_state or "running", result_metadata)
        print(
            f"[engine] charge done: state={result_state!r} meta={result_metadata}",
            flush=True,
        )

    except Exception as e:
        try:
            sender.release()
        except Exception:
            pass
        _publish_state(db, unit_id, "failed_error", {"error": str(e), "phase": "charging"})
        print(f"[engine] charge failed with exception: {e!r}", flush=True)
    finally:
        try:
            sender.close()
        except Exception:
            pass
        with _charge_target_lock:
            _charge_target_amps  = None
            _charge_applied_amps = 0.0
        _charge_active.clear()

    # Charge hit its duration cap. The loop above only released the motor;
    # the engine is still running with spark energized. Shut it down so it
    # doesn't idle indefinitely. This runs after the finally cleared
    # _charge_active, so handle_engine_stop skips the (self-)thread join and
    # just opens the spark relay + coasts to idle.
    if autostop_on_timeout:
        print("[engine] charge timed out — auto-stopping engine", flush=True)
        try:
            handle_engine_stop(db, unit_id, {})
        except Exception as e:
            print(f"[engine] auto-stop after charge timeout failed: {e!r}", flush=True)


def handle_engine_charge(
    db: firestore.Client,
    unit_id: str,
    payload: Dict[str, Any],
) -> None:
    """Spawn the background charge loop. Returns immediately.

    Pre-condition: engine should be running. We don't enforce — if it
    isn't, the charge loop will bail quickly on min-RPM check.

    payload (all optional):
      currentAmpsOverride
      voltageStopOverride
      maxDurationSecOverride
    """
    global _charge_thread

    if not _engine_lock.acquire(blocking=False):
        raise RuntimeError("engine.charge: another engine action in progress")
    try:
        if _charge_active.is_set():
            raise RuntimeError("engine.charge: already charging")

        cfg    = _load_charge_config(db, unit_id)
        params = _resolve_charge_params(cfg, payload)

        _charge_abort.clear()
        _charge_active.set()
        _charge_thread = threading.Thread(
            target=_run_charge_loop,
            args=(db, unit_id, params),
            daemon=True,
            name="engine-charge",
        )
        _charge_thread.start()
        print(
            f"[engine] charge spawned: targetAmps={params['current_amps']} "
            f"voltageStop={params['voltage_stop']} max={params['max_duration_sec']}s",
            flush=True,
        )
    finally:
        _engine_lock.release()


def handle_engine_charge_tune(
    db: firestore.Client,
    unit_id: str,
    payload: Dict[str, Any],
) -> None:
    """Adjust the live charge target while the loop is running.

    The charge thread reads `_charge_target_amps` every send tick and slews
    `_charge_applied_amps` toward it at MAX_SLEW_AMPS_PER_SEC. So calling
    this handler at any cadence is safe — the slew limiter does the rate
    limiting at the hardware-command level.

    payload:
      currentAmps — the new target (amps drawn from the motor, positive).
                    Clamped to [0, MAX_CHARGE_AMPS_HARD].

    Raises if no charge loop is active. Does NOT take _engine_lock — the
    charge loop doesn't hold it either, and we want tune commands to land
    even while the lock is briefly held by another caller.
    """
    global _charge_target_amps

    if not _charge_active.is_set():
        raise RuntimeError("engine.charge.tune: no charge loop active")

    raw = payload.get("currentAmps", payload.get("currentAmpsOverride"))
    if raw is None:
        raise ValueError("engine.charge.tune: missing currentAmps")
    new_target = max(0.0, min(MAX_CHARGE_AMPS_HARD, float(raw)))

    with _charge_target_lock:
        prev = _charge_target_amps
        _charge_target_amps = new_target

    _publish_state(db, unit_id, "charging", {
        "currentAmpsCommanded": new_target,
    })
    print(
        f"[engine] charge target tuned: {prev}A → {new_target}A "
        f"(slew {MAX_SLEW_AMPS_PER_SEC}A/s)",
        flush=True,
    )


# ─── stop primitive (phase G.4) ────────────────────────────────────────────

def _wait_for_rpm_idle(threshold: int, timeout_sec: float) -> Optional[int]:
    """After spark relay opens, watch the bus for RPM to fall under threshold.
    Returns the last RPM observed (or None if no STATUS_1 frame arrived)."""
    sender = VescSender(iface=VESC_IFACE, unit_id=VESC_UNIT_ID)
    try:
        sender.open()
        bus = sender._bus  # noqa: SLF001
        status1_id = (CMD_STATUS_1 << 8) | (sender.unit_id & 0xFF)
        deadline = time.monotonic() + timeout_sec
        last_rpm: Optional[int] = None
        while time.monotonic() < deadline:
            msg = bus.recv(timeout=0.2)
            if msg is None or not msg.is_extended_id or len(msg.data) < 8:
                continue
            if msg.arbitration_id != status1_id:
                continue
            last_rpm = struct.unpack(">i", msg.data[0:4])[0]
            if abs(last_rpm) <= threshold:
                return last_rpm
        return last_rpm
    finally:
        sender.close()


def handle_engine_stop(
    db: firestore.Client,
    unit_id: str,
    payload: Dict[str, Any],
) -> None:
    """Graceful engine shutdown:
      1. Signal any active charge loop to abort, wait for it.
      2. Turn off the spark relay.
      3. Watch RPM until it falls under rpmIdleThreshold or timeout.
      4. Publish state = "idle".

    Safe to call when the engine isn't actually running — just signals
    and de-energizes whatever's on. No payload options for v1.
    """
    # If a charge loop is running, signal abort WITHOUT holding _engine_lock:
    # the charge thread doesn't hold it either, and we need to give the
    # thread room to release the motor before we acquire the lock.
    if _charge_active.is_set():
        _charge_abort.set()
        if _charge_thread is not None:
            join_timeout = float(
                _load_stop_config(db, unit_id).get("chargeJoinTimeoutSec", 5)
            )
            _charge_thread.join(timeout=join_timeout)

    if not _engine_lock.acquire(blocking=False):
        raise RuntimeError("engine.stop: another engine action in progress")

    try:
        stop_cfg      = _load_stop_config(db, unit_id)
        spark_channel = int(stop_cfg.get("sparkRelayChannel", 2))
        idle_thresh   = int(stop_cfg.get("rpmIdleThreshold", 100))
        rpm_timeout   = float(stop_cfg.get("rpmWaitTimeoutSec", 15))

        _publish_state(db, unit_id, "stopping", {
            "startedAt": firestore.SERVER_TIMESTAMP,
        })

        try:
            _set_spark_safely(db, unit_id, spark_channel, "off")
        except Exception as e:
            print(f"[engine.stop] spark off failed: {e!r}", flush=True)

        final_rpm = _wait_for_rpm_idle(idle_thresh, rpm_timeout)

        _publish_state(db, unit_id, "idle", {
            "stoppedAt": firestore.SERVER_TIMESTAMP,
            "finalRpm":  final_rpm if final_rpm is not None else "unknown",
            "trigger":   "manual_stop",
        })
        print(f"[engine] stop complete: final_rpm={final_rpm}", flush=True)
    finally:
        _engine_lock.release()
