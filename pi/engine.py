"""
Engine start orchestrator (phase G.2).

The crank primitive: drive the VESC at a commanded motor current for up to
maxDurationSec, refreshing the command at refreshHz to keep the VESC's
command-timeout watchdog satisfied. Aborts when the engine catches OR when
the duration ceiling is hit.

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

from vesc_listener import CMD_STATUS_1
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

# Module-level mutex prevents two crank commands from racing on the same bus.
_crank_lock = threading.Lock()


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


# ─── command handler ───────────────────────────────────────────────────────

def handle_engine_crank(
    db: firestore.Client,
    unit_id: str,
    payload: Dict[str, Any],
) -> None:
    """Run one crank attempt.

    payload (all optional):
      currentAmpsOverride   — override config/engine.cranking.currentAmps
      maxDurationSecOverride — override config/engine.cranking.maxDurationSec

    Both overrides are clamped to the hard safety ceilings.
    """
    # Prevent two cranks from racing. Reject the second instead of queueing.
    if not _crank_lock.acquire(blocking=False):
        raise RuntimeError("engine.crank: already in progress")

    try:
        cfg = _load_crank_config(db, unit_id)

        current_amps = float(
            payload.get("currentAmpsOverride", cfg.get("currentAmps", 65))
        )
        max_dur = float(
            payload.get("maxDurationSecOverride", cfg.get("maxDurationSec", 4))
        )
        refresh_hz = int(cfg.get("refreshHz", 10))
        catch_ratio = float(cfg.get("catchCurrentRatio", DEFAULT_CATCH_CURRENT_RATIO))
        catch_hold_ms = int(cfg.get("catchHoldMs", 200))
        arm_ratio = float(cfg.get("armCurrentRatio", 0.5))

        # Clamp to hard safety ceilings.
        current_amps = max(0.0, min(MAX_CURRENT_AMPS_HARD, current_amps))
        max_dur      = max(MIN_DURATION_SEC_HARD, min(MAX_DURATION_SEC_HARD, max_dur))
        refresh_hz   = max(MIN_REFRESH_HZ, min(MAX_REFRESH_HZ, refresh_hz))

        _publish_state(db, unit_id, "cranking", {
            "startedAt": firestore.SERVER_TIMESTAMP,
            "currentAmpsCommanded": current_amps,
            "maxDurationSec": max_dur,
            "refreshHz": refresh_hz,
        })

        sender = VescSender(iface=VESC_IFACE, unit_id=VESC_UNIT_ID)
        sender.open()
        # Reuse the sender's bus for reads too — socketcan is fine with
        # mixed read/write on the same handle.
        bus = sender._bus  # noqa: SLF001  (intentional same-bus access)

        try:
            result = _run_crank_loop(
                sender, bus,
                current_amps=current_amps,
                max_duration_sec=max_dur,
                refresh_hz=refresh_hz,
                catch_current_ratio=catch_ratio,
                catch_hold_ms=catch_hold_ms,
                arm_current_ratio=arm_ratio,
            )
        finally:
            # Belt-and-suspenders: even if the loop raised, ensure one final
            # release goes out before we close the bus.
            try:
                sender.release()
            except Exception:
                pass
            sender.close()

        _publish_state(db, unit_id, result.state, result.metadata)
        print(
            f"[engine] crank done: state={result.state!r} "
            f"meta={result.metadata}",
            flush=True,
        )

    except Exception as e:
        _publish_state(db, unit_id, "failed_error", {"error": str(e)})
        print(f"[engine] crank failed with exception: {e!r}", flush=True)
        raise
    finally:
        _crank_lock.release()
