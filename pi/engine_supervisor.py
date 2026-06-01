"""
Autonomous engine supervisor (phase G.5).

Background thread that monitors pack voltage and orchestrates the
start → charge → stop cycle hands-off. Lives in the command_listener
process so it can call engine.* handlers directly (no Firestore command
round-trip).

Decision logic per tick
-----------------------
Reads `current/snapshot.motor_volts` and `current/engine.state`, then:

  enabled == false                                  → no-op (manual mode)
  in cooldown after recent action                   → no-op (back-off window)
  state == starting | cranking | stopping           → no-op (action in flight)
  state startswith "failed"                         → no-op (manual review)
  state == charging                                 → no-op (charge loop owns it)

  state == idle, voltage <= voltageCritical,
    AND (quiet hours OFF OR allowQuietOverride)     → auto-start cycle
  state == idle, voltage <= voltageStart, no quiet  → auto-start cycle

  state == running, voltage >= voltageStop          → auto-stop
  state == running, voltage <  voltageStop          → auto-charge

Auto-start cycle blocks on handle_engine_start until catch or failure,
then immediately fires handle_engine_charge (which spawns the bg charge
loop and returns).

Coexists with manual mode
-------------------------
The supervisor uses the same engine handlers + the same _engine_lock as
the app's manual buttons. If the user manually starts an engine, the
supervisor sees state=running and will auto-charge it (assuming pack
needs charging). If the user manually stops mid-cycle, supervisor sees
state=idle and may restart (subject to thresholds + cooldown). Disable
the supervisor (`config/engine.supervisor.enabled = false`) for pure
manual control.

Events go to `units/{u}/events` so the app's existing event feed shows
auto-actions alongside user-initiated ones.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from firebase_admin import firestore


# ─── defaults ──────────────────────────────────────────────────────────────

DEFAULT_SUPERVISOR_CONFIG: Dict[str, Any] = {
    # Opt-in. Set true in Firestore (or via a future app toggle) to enable.
    "enabled":            False,
    # Voltage thresholds for 14S LiFePO4 (~46.5 V ≈ 20 % SOC,
    # ~45.5 V ≈ 10 % SOC). Tune empirically vs the LCD's SOC reading.
    "voltageStart":       46.5,   # below this → auto-start (in active window)
    "voltageCritical":    45.5,   # below this → start regardless of quiet hours
                                  # (if allowQuietOverride is true)
    # How often the supervisor evaluates state. Voltage doesn't change
    # fast so 15-30s is plenty. Tick lower during development for faster
    # feedback.
    "tickIntervalSec":    15,
    # After firing any auto-action, ignore further triggers for this long
    # so we don't double-fire while state is settling. Should be longer
    # than the longest expected action (engine start ≈ 6-10s).
    "actionCooldownSec":  60,
    # Honor config/charge.quietHours when deciding whether to start.
    # voltageCritical override still applies if allowQuietOverride=true.
    "respectQuietHours":  True,
}


# ─── helpers ───────────────────────────────────────────────────────────────

def _is_in_quiet_hours(start_str: str, end_str: str, now: datetime) -> bool:
    """`start_str`/`end_str` are "HH:MM" 24-hour. Spans midnight when
    end <= start (e.g. 21:00–07:00). Local time per the Pi's clock."""
    try:
        sh, sm = (int(p) for p in start_str.split(":"))
        eh, em = (int(p) for p in end_str.split(":"))
    except Exception:
        return False
    now_min = now.hour * 60 + now.minute
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= now_min < end_min
    # spans midnight
    return now_min >= start_min or now_min < end_min


# ─── supervisor class ──────────────────────────────────────────────────────

class EngineSupervisor:
    def __init__(self, db: firestore.Client, unit_id: str) -> None:
        self.db = db
        self.unit_id = unit_id
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_action_at: float = 0.0  # monotonic
        # Track whether we've emitted the "enabled" / "disabled" log line
        # so we don't spam on every tick.
        self._last_enabled_state: Optional[bool] = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="engine-supervisor"
        )
        self._thread.start()
        print(
            "[supervisor] thread started; config/engine.supervisor.enabled gates actions",
            flush=True,
        )

    def stop(self) -> None:
        self._stop_event.set()

    # ── main loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                cfg = self._load_supervisor_config()
                enabled = bool(cfg.get("enabled", False))
                if enabled != self._last_enabled_state:
                    print(
                        f"[supervisor] enabled={enabled}",
                        flush=True,
                    )
                    self._last_enabled_state = enabled
                if enabled:
                    self._tick(cfg)
            except Exception as e:
                # Never let a per-tick error kill the thread.
                print(f"[supervisor] tick error: {e!r}", flush=True)

            tick = max(5, int(cfg.get("tickIntervalSec", 15)))
            if self._stop_event.wait(tick):
                return

    # ── per-tick logic ────────────────────────────────────────────────────

    def _tick(self, cfg: Dict[str, Any]) -> None:
        if self._in_cooldown(cfg):
            return

        state, voltage = self._read_engine_and_voltage()
        if voltage is None:
            # No telemetry — wait for the publisher to populate.
            return

        # Action-in-flight states: don't touch anything.
        if state in ("starting", "cranking", "stopping"):
            return
        # Failure states need manual review — supervisor doesn't auto-recover.
        if state and state.startswith("failed"):
            return
        # Charge loop owns its lifetime.
        if state == "charging":
            return

        v_start    = float(cfg.get("voltageStart",    46.5))
        v_critical = float(cfg.get("voltageCritical", 45.5))
        v_stop     = float(self._load_voltage_stop())

        in_quiet = bool(cfg.get("respectQuietHours", True)) and self._is_quiet_hours()
        allow_quiet_override = self._load_allow_quiet_override()

        if state in (None, "idle"):
            if voltage <= v_critical and (not in_quiet or allow_quiet_override):
                self._auto_start_cycle(voltage, v_critical, "critical")
            elif voltage <= v_start and not in_quiet:
                self._auto_start_cycle(voltage, v_start, "normal")
            return

        if state == "running":
            if voltage >= v_stop:
                self._auto_stop(voltage, v_stop)
            else:
                # Engine is running, pack still below stop voltage → load it.
                self._auto_charge(voltage, v_stop)
            return

        # Unknown state — log once and back off.
        print(f"[supervisor] unrecognized engine state: {state!r}; skipping tick", flush=True)

    # ── auto-actions ──────────────────────────────────────────────────────

    def _auto_start_cycle(self, voltage: float, threshold: float, reason: str) -> None:
        """engine.start (synchronous) → if caught, engine.charge (async).
        Single-action with a chained charge so we're not waiting an entire
        tick interval to begin loading the engine."""
        # Import lazily so this module imports cleanly on a Mac without
        # the Pi hardware deps.
        from engine import handle_engine_start, handle_engine_charge

        self._log_event(
            "engine.auto_start",
            f"Auto-starting engine (voltage {voltage:.1f}V ≤ {threshold:.1f}V, {reason})",
            "info",
            {"voltage": voltage, "threshold": threshold, "reason": reason},
        )
        self._last_action_at = time.monotonic()
        try:
            handle_engine_start(self.db, self.unit_id, {})
        except Exception as e:
            self._log_event(
                "engine.auto_start_failed",
                f"Auto-start raised: {e}",
                "warning",
                {"voltage": voltage, "error": str(e)},
            )
            print(f"[supervisor] auto_start failed: {e!r}", flush=True)
            return

        # Did it catch?
        state, _ = self._read_engine_and_voltage()
        if state != "running":
            # start macro publishes the failure state itself; we just log
            # that the catch never happened so the event feed has both signals.
            self._log_event(
                "engine.auto_start_no_catch",
                f"Engine.start completed but state={state!r} (no catch)",
                "warning",
                {"endState": state},
            )
            return

        # Engine running — kick off the charge immediately.
        try:
            handle_engine_charge(self.db, self.unit_id, {})
            self._log_event(
                "engine.auto_charge",
                f"Auto-charging after start (target {self._load_voltage_stop():.1f}V)",
                "info",
                {"voltage": voltage, "voltageStop": self._load_voltage_stop()},
            )
        except Exception as e:
            self._log_event(
                "engine.auto_charge_failed",
                f"Auto-charge raised: {e}",
                "warning",
                {"voltage": voltage, "error": str(e)},
            )
            print(f"[supervisor] auto_charge after start failed: {e!r}", flush=True)

    def _auto_charge(self, voltage: float, v_stop: float) -> None:
        """Engine is already running unloaded → load it."""
        from engine import handle_engine_charge

        self._last_action_at = time.monotonic()
        try:
            handle_engine_charge(self.db, self.unit_id, {})
            self._log_event(
                "engine.auto_charge",
                f"Auto-charging (voltage {voltage:.1f}V; target {v_stop:.1f}V)",
                "info",
                {"voltage": voltage, "voltageStop": v_stop},
            )
        except Exception as e:
            # Most common: "already charging" — benign, just don't log noisily.
            msg = str(e)
            if "already charging" in msg:
                return
            self._log_event(
                "engine.auto_charge_failed",
                f"Auto-charge raised: {e}",
                "warning",
                {"voltage": voltage, "error": msg},
            )
            print(f"[supervisor] auto_charge failed: {e!r}", flush=True)

    def _auto_stop(self, voltage: float, threshold: float) -> None:
        from engine import handle_engine_stop

        self._last_action_at = time.monotonic()
        try:
            self._log_event(
                "engine.auto_stop",
                f"Auto-stopping (voltage {voltage:.1f}V ≥ {threshold:.1f}V)",
                "info",
                {"voltage": voltage, "threshold": threshold},
            )
            handle_engine_stop(self.db, self.unit_id, {})
        except Exception as e:
            self._log_event(
                "engine.auto_stop_failed",
                f"Auto-stop raised: {e}",
                "warning",
                {"voltage": voltage, "error": str(e)},
            )
            print(f"[supervisor] auto_stop failed: {e!r}", flush=True)

    # ── readers ───────────────────────────────────────────────────────────

    def _load_supervisor_config(self) -> Dict[str, Any]:
        cfg = dict(DEFAULT_SUPERVISOR_CONFIG)
        try:
            snap = self.db.document(f"units/{self.unit_id}/config/engine").get()
            if snap.exists:
                block = (snap.to_dict() or {}).get("supervisor", {})
                cfg.update(block)
        except Exception as e:
            print(f"[supervisor] config read failed: {e!r}", flush=True)
        return cfg

    def _load_voltage_stop(self) -> float:
        try:
            snap = self.db.document(f"units/{self.unit_id}/config/engine").get()
            if snap.exists:
                charge_block = (snap.to_dict() or {}).get("charge", {})
                return float(charge_block.get("voltageStop", 54.0))
        except Exception:
            pass
        return 54.0

    def _load_allow_quiet_override(self) -> bool:
        try:
            snap = self.db.document(f"units/{self.unit_id}/config/charge").get()
            if snap.exists:
                return bool((snap.to_dict() or {}).get("allowQuietOverride", False))
        except Exception:
            pass
        return False

    def _is_quiet_hours(self) -> bool:
        try:
            snap = self.db.document(f"units/{self.unit_id}/config/charge").get()
            if not snap.exists:
                return False
            qh = (snap.to_dict() or {}).get("quietHours", {})
            start, end = qh.get("start"), qh.get("end")
            if not start or not end:
                return False
            return _is_in_quiet_hours(start, end, datetime.now())
        except Exception:
            return False

    def _read_engine_and_voltage(self) -> Tuple[Optional[str], Optional[float]]:
        state: Optional[str] = None
        voltage: Optional[float] = None
        try:
            engine_snap = self.db.document(f"units/{self.unit_id}/current/engine").get()
            if engine_snap.exists:
                state = (engine_snap.to_dict() or {}).get("state")
        except Exception:
            pass
        try:
            snap = self.db.document(f"units/{self.unit_id}/current/snapshot").get()
            if snap.exists:
                v = (snap.to_dict() or {}).get("motor_volts")
                if isinstance(v, (int, float)):
                    voltage = float(v)
        except Exception:
            pass
        return state, voltage

    def _in_cooldown(self, cfg: Dict[str, Any]) -> bool:
        cooldown_sec = float(cfg.get("actionCooldownSec", 60))
        return (time.monotonic() - self._last_action_at) < cooldown_sec

    # ── event logging ─────────────────────────────────────────────────────

    def _log_event(
        self,
        type_: str,
        message: str,
        severity: str,
        data: Dict[str, Any],
    ) -> None:
        try:
            self.db.collection(f"units/{self.unit_id}/events").add({
                "type":      type_,
                "severity":  severity,
                "message":   message,
                "data":      data,
                "createdAt": firestore.SERVER_TIMESTAMP,
                "source":    "supervisor",
            })
        except Exception as e:
            print(f"[supervisor] event log failed for {type_}: {e!r}", flush=True)


# ─── module-level singleton + entrypoint for command_listener ─────────────

_supervisor: Optional[EngineSupervisor] = None


def start_supervisor(db: firestore.Client, unit_id: str) -> None:
    """Idempotent: spawn the supervisor thread on first call, no-op after."""
    global _supervisor
    if _supervisor is not None and _supervisor._thread is not None and _supervisor._thread.is_alive():
        return
    _supervisor = EngineSupervisor(db, unit_id)
    _supervisor.start()


def stop_supervisor() -> None:
    """Signal the supervisor thread to exit (used on listener shutdown)."""
    global _supervisor
    if _supervisor is not None:
        _supervisor.stop()
