"""
SitePulse engine recharge scheduler.

Every SITEPULSE_SCHEDULER_INTERVAL seconds (default 30):
  1. Read SoC from `units/{UNIT_ID}/current/snapshot.battery_soc`.
  2. Read user config from `units/{UNIT_ID}/config/charge`.
  3. Evaluate `(now, soc, config)` → `(desired, reason)`.
  4. If desired flips, log an `engine.start` / `engine.stop` event and call
     the VESC stub. The real CAN frame goes in `_vesc_set_current` later.
  5. Write `units/{UNIT_ID}/current/engine` so the app can render.

Quiet-hours behavior matches the locked-in plan: when SoC drops to
`socCritical` inside quiet hours and `allowQuietOverride` is false, we
emit a one-per-30min `soc.critical` event. The push-fanout function
sends a notification the user can tap to issue a one-shot quiet override
via the `engine.override` command with action='allow_quiet_once'.

Manual overrides:
  - `engine.override` `{action: 'run_now'}`    → desired='run' next cycle
  - `engine.override` `{action: 'stop'}`       → desired='idle' next cycle
  - `engine.override` `{action: 'allow_quiet_once'}`
       → grants `allowQuietOverride` until the next `quietHours.end`

All wall-clock evaluation runs in the unit's `regionTimezone` (from
the unit doc; defaults to `SITEPULSE_DEFAULT_TZ`, fallback `America/Denver`).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[assignment]

from firebase_admin import firestore


SCHEDULER_INTERVAL_S = float(os.environ.get("SITEPULSE_SCHEDULER_INTERVAL", "30"))
DEFAULT_TZ           = os.environ.get("SITEPULSE_DEFAULT_TZ", "America/Denver")


# ─── VESC engine stub ───────────────────────────────────────────────────────
# When the CAN frame for the VESC custom engine controller is known,
# replace the body of this function. Everything else in the file stays put.

def _vesc_set_current(amps: float) -> None:
    """STUB — real implementation sends a CAN frame to the VESC engine
    controller. Logged for now so we can verify the scheduler's decisions
    in the publisher log without driving hardware."""
    print(f"[scheduler] STUB vesc_set_current({amps:+.1f} A)", flush=True)


# ─── time helpers ───────────────────────────────────────────────────────────

def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(hour=int(h), minute=int(m))


def _zone_for_unit(db: firestore.Client, unit_id: str) -> str:
    try:
        snap = db.document(f"units/{unit_id}").get()
        if snap.exists:
            tz = snap.get("regionTimezone")
            if isinstance(tz, str) and tz:
                return tz
    except Exception:
        pass
    return DEFAULT_TZ


def _now_in(zone: str) -> datetime:
    if ZoneInfo is None:
        return datetime.now(timezone.utc)
    try:
        return datetime.now(ZoneInfo(zone))
    except Exception:
        return datetime.now(timezone.utc)


def _in_window(now: datetime, start: dtime, end: dtime) -> bool:
    """[start, end) — handles wrap-past-midnight (start > end)."""
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _weekday(now: datetime) -> int:
    """0-6 with Sunday = 0, matching the JS / ChargeWindow.weekdays convention."""
    # Python: 0=Mon..6=Sun. Convert.
    return (now.weekday() + 1) % 7


# ─── evaluator ──────────────────────────────────────────────────────────────

def _evaluate(
    now: datetime,
    soc: Optional[float],
    config: Dict[str, Any],
    prior_desired: str,
) -> Tuple[str, str]:
    """Return (desired, reason). Pure function — easy to unit-test later."""
    if not config.get("enabled", False):
        return ("idle", "disabled")

    soc_critical          = float(config.get("socCritical", 12))
    soc_start             = float(config.get("socStart", 25))
    soc_stop              = float(config.get("socStop", 85))
    allow_quiet_override  = bool(config.get("allowQuietOverride", False))

    quiet = config.get("quietHours") or {}
    in_quiet = False
    if quiet.get("start") and quiet.get("end"):
        try:
            in_quiet = _in_window(
                now, _parse_hhmm(quiet["start"]), _parse_hhmm(quiet["end"])
            )
        except Exception:
            in_quiet = False

    if in_quiet and not allow_quiet_override:
        return ("idle", "quiet_hours")

    today = _weekday(now)
    in_window = False
    for w in (config.get("windows") or []):
        weekdays = w.get("weekdays") or []
        if weekdays and today not in weekdays:
            continue
        try:
            if _in_window(now, _parse_hhmm(w["start"]), _parse_hhmm(w["end"])):
                in_window = True
                break
        except Exception:
            continue
    if not in_window:
        return ("idle", "window_closed")

    if soc is None:
        # No telemetry — fail safe to idle. Don't run the engine blind.
        return ("idle", "disabled")

    if soc <= soc_start:
        return ("run", "soc_low")
    if soc >= soc_stop:
        return ("idle", "soc_satisfied")

    # Hysteresis between socStart and socStop — hold whatever we were doing.
    if prior_desired == "run":
        return ("run", "soc_low")
    return ("idle", "soc_satisfied")


# ─── manual override state ──────────────────────────────────────────────────

_override_lock = threading.Lock()
_override_action: Optional[str] = None
_quiet_override_until: Optional[datetime] = None


# ─── command handlers (called by command_listener.py via lazy import) ──────

def handle_engine_override(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """payload = {action: 'run_now' | 'stop' | 'allow_quiet_once'}"""
    global _override_action, _quiet_override_until
    action = payload.get("action")
    if action not in ("run_now", "stop", "allow_quiet_once"):
        raise ValueError(f"engine.override: invalid action {action!r}")

    if action == "allow_quiet_once":
        zone = _zone_for_unit(db, unit_id)
        cfg = db.document(f"units/{unit_id}/config/charge").get()
        quiet = (cfg.to_dict() or {}).get("quietHours") or {}
        end_s = quiet.get("end", "07:00")
        try:
            end_t = _parse_hhmm(end_s)
        except Exception:
            end_t = dtime(7, 0)
        now = _now_in(zone)
        until = now.replace(hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0)
        if until <= now:
            until += timedelta(days=1)
        with _override_lock:
            _quiet_override_until = until
        return

    with _override_lock:
        _override_action = action


def handle_charge_update(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """payload = {configPatch: Partial<ChargeConfig>}"""
    patch = payload.get("configPatch")
    if not isinstance(patch, dict):
        raise ValueError("charge.update: configPatch must be an object")
    db.document(f"units/{unit_id}/config/charge").set(patch, merge=True)


# ─── sentry-triggered scare pulse ──────────────────────────────────────────
# Called from pi/sentry.py when motion fires and config/sentry.scareMode is
# on. Fires the engine for `duration_s` seconds regardless of schedule, then
# stops it. 5-minute cooldown so a stream of motion triggers doesn't cycle
# the engine to death.

_scare_lock = threading.Lock()
_scare_active = False
_last_scare_t: Optional[float] = None
SCARE_COOLDOWN_S = 5 * 60


def trigger_scare_pulse(db: firestore.Client, unit_id: str, duration_s: int = 20) -> bool:
    """Returns True if the scare was fired, False if suppressed (already
    running, or within cooldown). Non-blocking — work happens on a thread."""
    global _scare_active, _last_scare_t

    with _scare_lock:
        if _scare_active:
            return False
        now = time.monotonic()
        if _last_scare_t and (now - _last_scare_t) < SCARE_COOLDOWN_S:
            return False
        _scare_active = True
        _last_scare_t = now

    duration_s = max(3, min(60, int(duration_s)))   # clamp [3, 60] seconds

    def pulse() -> None:
        global _scare_active
        try:
            db.collection(f"units/{unit_id}/events").add({
                "kind": "engine.start",
                "at": firestore.SERVER_TIMESTAMP,
                "source": "pi",
                "payload": {"reason": "sentry_scare", "duration_s": duration_s},
            })
            amps = float(os.environ.get("SITEPULSE_ENGINE_CURRENT", "30"))
            _vesc_set_current(amps)
            time.sleep(duration_s)
        except Exception as e:
            print(f"[scheduler] scare pulse error: {e}", flush=True)
        finally:
            try:
                _vesc_set_current(0.0)
            except Exception:
                pass
            try:
                db.collection(f"units/{unit_id}/events").add({
                    "kind": "engine.stop",
                    "at": firestore.SERVER_TIMESTAMP,
                    "source": "pi",
                    "payload": {"reason": "sentry_scare_complete"},
                })
            except Exception:
                pass
            with _scare_lock:
                _scare_active = False

    threading.Thread(target=pulse, daemon=True, name="scare-pulse").start()
    return True


# ─── scheduler loop ─────────────────────────────────────────────────────────

_scheduler_thread: Optional[threading.Thread] = None
_prior_desired = "idle"
_prior_critical_event_t: Optional[float] = None
_CRITICAL_EVENT_COOLDOWN_S = 30 * 60   # one notification per 30 minutes max


def _emit_critical_event(db: firestore.Client, unit_id: str, soc: float) -> None:
    """Append a soc.critical event, rate-limited."""
    global _prior_critical_event_t
    now = time.monotonic()
    if _prior_critical_event_t and (now - _prior_critical_event_t) < _CRITICAL_EVENT_COOLDOWN_S:
        return
    _prior_critical_event_t = now
    db.collection(f"units/{unit_id}/events").add({
        "kind": "soc.critical",
        "at": firestore.SERVER_TIMESTAMP,
        "source": "pi",
        "payload": {"soc": soc},
    })


def _emit_state_change_event(db: firestore.Client, unit_id: str, kind: str, reason: str) -> None:
    db.collection(f"units/{unit_id}/events").add({
        "kind": kind,
        "at": firestore.SERVER_TIMESTAMP,
        "source": "pi",
        "payload": {"reason": reason},
    })


def start_scheduler(db: firestore.Client, unit_id: str) -> threading.Thread:
    """Start (once) the scheduler thread. Idempotent — repeat calls return
    the same thread object."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return _scheduler_thread

    def loop() -> None:
        global _prior_desired, _override_action, _quiet_override_until
        while True:
            try:
                zone = _zone_for_unit(db, unit_id)

                cfg_snap = db.document(f"units/{unit_id}/config/charge").get()
                config = cfg_snap.to_dict() if cfg_snap.exists else {}

                tel_snap = db.document(f"units/{unit_id}/current/snapshot").get()
                soc: Optional[float] = None
                if tel_snap.exists:
                    raw = tel_snap.get("battery_soc")
                    if isinstance(raw, (int, float)):
                        soc = float(raw)

                now = _now_in(zone)

                # One-shot quiet override granted via push action.
                with _override_lock:
                    if _quiet_override_until and now < _quiet_override_until:
                        config = {**config, "allowQuietOverride": True}
                    else:
                        _quiet_override_until = None

                desired, reason = _evaluate(now, soc, config, _prior_desired)

                # Per-cycle manual override beats the evaluator.
                with _override_lock:
                    if _override_action == "run_now":
                        desired, reason = ("run", "manual_override")
                        _override_action = None
                    elif _override_action == "stop":
                        desired, reason = ("idle", "manual_override")
                        _override_action = None

                # Critical-SoC alert — only when quiet hours are blocking the run.
                if (
                    soc is not None
                    and soc <= float(config.get("socCritical", 12))
                    and reason == "quiet_hours"
                ):
                    _emit_critical_event(db, unit_id, soc)

                # Drive the engine on state transitions.
                if desired != _prior_desired:
                    if desired == "run":
                        _vesc_set_current(float(config.get("engineCurrent", 30)))
                        _emit_state_change_event(db, unit_id, "engine.start", reason)
                    else:
                        _vesc_set_current(0.0)
                        _emit_state_change_event(db, unit_id, "engine.stop", reason)
                    _prior_desired = desired

                # Mirror the decision so the app can render the live state row.
                db.document(f"units/{unit_id}/current/engine").set({
                    "desired": desired,
                    "reason": reason,
                    "lastEvalAt": firestore.SERVER_TIMESTAMP,
                    "socAtEval": soc,
                }, merge=True)

            except Exception as e:
                print(f"[scheduler] eval error: {e}", flush=True)

            time.sleep(SCHEDULER_INTERVAL_S)

    _scheduler_thread = threading.Thread(
        target=loop, daemon=True, name="charge-scheduler",
    )
    _scheduler_thread.start()
    return _scheduler_thread
