"""
Autonomous engine supervisor (phase G.5).

Background thread that monitors pack voltage and orchestrates the
start → charge → stop cycle hands-off. Lives in the command_listener
process so it can call engine.* handlers directly (no Firestore command
round-trip).

Decision logic per tick
-----------------------
Each tick wakes the Predator LCD (so battery_soc and time_to_empty are
fresh), reads the snapshot + engine state, resolves SoC from the best
available signal (LCD coulomb-counted = primary, VESC voltage-derived =
fallback), then evaluates:

  enabled == false                                  → no-op (manual mode)
  in cooldown after recent action                   → no-op (back-off window)
  state == starting | cranking | stopping           → no-op (action in flight)
  state startswith "failed"                         → no-op (manual review)
  state == charging                                 → no-op (charge loop owns it)

  state == idle, soc <= socCritical,
    AND (quiet hours OFF OR allowQuietOverride)     → auto-start ("critical_soc")
  state == idle, soc <= socStart, no quiet          → auto-start ("low_soc")
  state == idle, ttempty <= runwayThresholdMin
    AND soc <= proactiveSocCeiling, no quiet        → auto-start ("load_aware")

  state == running, soc >= socStop,
    AND (ttempty is None OR ttempty >= sustainableRunwayMin)
                                                    → auto-stop
  state == running, soc >= socStop, load is heavy   → keep running (defer stop)
  state == running, soc <  socStop                  → auto-charge

The "load-aware" branch is what prevents the scenario where a heavy AC
load drains the pack while we wait for the SoC threshold — by then the
engine can no longer keep up. ttempty is the Predator BMS's load-aware
runway prediction (in minutes) and is the cleanest signal we have.

The "keep running" branch is the converse: don't stop the engine just
because pack hit 85% if there's still a heavy load — we'd just have to
restart immediately.

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
    "enabled":               False,

    # Primary decision signal: Predator LCD coulomb-counted SoC (0-100 %).
    # Used when battery_soc is present in the snapshot.
    "socStart":              25,    # ≤ this → auto-start (in active window)
    "socCritical":           12,    # ≤ this → start regardless of quiet hours
    "socStop":               85,    # ≥ this → auto-stop (subject to load guard)

    # Load-aware proactive start: if the Predator BMS's runway estimate
    # (time_to_empty_minutes) drops below threshold AND SoC is also at
    # least somewhat depleted, fire the engine BEFORE we actually hit
    # socStart. Prevents the "pack drained before engine could catch up"
    # scenario under sustained heavy load.
    "runwayThresholdMin":    60,    # if runway ≤ this → consider proactive start
    "proactiveSocCeiling":   60,    # but ONLY if SoC ≤ this (no fires at 95%)

    # Load-aware stop guard: don't auto-stop the engine if the BMS runway
    # under current load is short — we'd just have to immediately restart.
    "sustainableRunwayMin":  180,   # only stop if runway ≥ this OR no load

    # Voltage fallback for when LCD is asleep AND battery_soc is null in
    # the snapshot. Curve is 15S LiFePO4; see _volts_to_soc_15s below
    # and src/lib/voltageSoc.ts for the matching display curve.
    "voltageStart":          48.0,
    "voltageCritical":       46.5,

    # How often the supervisor evaluates state. Each tick wakes the LCD
    # before reading, so don't tick too aggressively or the servo wears
    # out. 15-30s is plenty.
    "tickIntervalSec":       15,
    # After firing any auto-action, ignore further triggers for this long.
    "actionCooldownSec":     60,
    # Honor config/charge.quietHours when deciding whether to start.
    # socCritical override still applies if allowQuietOverride=true.
    "respectQuietHours":     True,
}


# Voltage → SoC curve for the 15S LiFePO4 pack. Mirror of CURVE in
# src/lib/voltageSoc.ts — keep both in sync if you adjust either. This
# is the SoC fallback when Predator LCD is unavailable (asleep, servo
# failed). Note it's voltage-only and doesn't compensate for load IR
# drop, so under heavy load it will read low; that's why the LCD path
# is primary.
_SOC_CURVE_15S = (
    (45.0,   0),
    (46.5,  10),
    (48.0,  20),
    (48.5,  30),
    (49.0,  50),
    (49.5,  70),
    (50.25, 80),
    (51.5,  90),
    (52.5,  95),
    (53.25, 100),
)


def _volts_to_soc_15s(v: float) -> int:
    """Linearly interpolate the 15S LiFePO4 voltage→SoC curve. Clamps to
    [0, 100] outside the curve range."""
    if v <= _SOC_CURVE_15S[0][0]:
        return 0
    if v >= _SOC_CURVE_15S[-1][0]:
        return 100
    for i in range(1, len(_SOC_CURVE_15S)):
        hi_v, hi_s = _SOC_CURVE_15S[i]
        if v <= hi_v:
            lo_v, lo_s = _SOC_CURVE_15S[i - 1]
            t = (v - lo_v) / (hi_v - lo_v)
            return round(lo_s + t * (hi_s - lo_s))
    return 100  # unreachable; satisfies type checker


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

        # Pre-wake the Predator LCD so battery_soc and time_to_empty are
        # fresh at decision time. Best effort — if the servo fails, we
        # fall through to whatever's in the snapshot.
        self._pre_wake_lcd()

        state, telem = self._read_engine_telemetry()

        # Action-in-flight states: don't touch anything.
        if state in ("starting", "cranking", "stopping"):
            return
        # Failure states need manual review — supervisor doesn't auto-recover.
        if state and state.startswith("failed"):
            return
        # Charge loop owns its lifetime.
        if state == "charging":
            return

        # Resolve SoC from best available source.
        soc, soc_source = self._resolve_soc(telem)
        if soc is None:
            # Neither Predator nor VESC fallback has data — wait next tick.
            return

        soc_start    = int(cfg.get("socStart",    25))
        soc_critical = int(cfg.get("socCritical", 12))
        soc_stop     = int(cfg.get("socStop",     85))
        runway_thr   = int(cfg.get("runwayThresholdMin",    60))
        proactive_lim = int(cfg.get("proactiveSocCeiling",  60))
        sustainable  = int(cfg.get("sustainableRunwayMin", 180))

        in_quiet = bool(cfg.get("respectQuietHours", True)) and self._is_quiet_hours()
        allow_quiet_override = self._load_allow_quiet_override()

        ttempty = telem.get("ttempty")  # minutes, or None

        if state in (None, "idle"):
            # 1. Critical SoC — fires even in quiet hours if user allowed it.
            if soc <= soc_critical and (not in_quiet or allow_quiet_override):
                self._auto_start_cycle(soc, soc_source, ttempty, "critical_soc")
                return
            # 2. Normal low-SoC trigger.
            if soc <= soc_start and not in_quiet:
                self._auto_start_cycle(soc, soc_source, ttempty, "low_soc")
                return
            # 3. Load-aware proactive start: short runway AND already somewhat
            #    depleted. The ceiling prevents firing on transient spikes
            #    at high SoC (e.g. brief vacuum or power-tool use at 95%).
            if (
                ttempty is not None
                and ttempty <= runway_thr
                and soc <= proactive_lim
                and not in_quiet
            ):
                self._auto_start_cycle(soc, soc_source, ttempty, "load_aware")
                return
            return

        if state == "running":
            if soc >= soc_stop:
                # Pack is full; should we actually stop, or is load too heavy?
                load_ok = ttempty is None or ttempty >= sustainable
                if load_ok:
                    self._auto_stop(soc, soc_source, ttempty)
                else:
                    # Defer stop — heavy load means we'd immediately restart.
                    # Charge loop will naturally back off SET_CURRENT as
                    # voltage settles near voltageStop; engine idles + carries
                    # the load directly.
                    self._note_keep_running(soc, ttempty, sustainable)
                return
            # SoC below stop threshold → continue (or start) charging.
            self._auto_charge(soc, soc_source)
            return

        # Unknown state — log once and back off.
        print(f"[supervisor] unrecognized engine state: {state!r}; skipping tick", flush=True)

    # ── auto-actions ──────────────────────────────────────────────────────

    def _auto_start_cycle(
        self,
        soc: int,
        soc_source: str,
        ttempty: Optional[int],
        reason: str,
    ) -> None:
        """engine.start (synchronous) → if caught, engine.charge (async).
        Single-action with a chained charge so we're not waiting an entire
        tick interval to begin loading the engine.

        `reason` is one of: critical_soc, low_soc, load_aware. Goes into
        the event feed so the user can see why each auto-start fired.
        """
        # Import lazily so this module imports cleanly on a Mac without
        # the Pi hardware deps.
        from engine import handle_engine_start, handle_engine_charge

        msg_runway = f", runway={ttempty}m" if ttempty is not None else ""
        self._log_event(
            "engine.auto_start",
            f"Auto-starting engine (soc={soc}% via {soc_source}{msg_runway}, reason={reason})",
            "info",
            {
                "soc":        soc,
                "socSource":  soc_source,
                "ttempty":    ttempty,
                "reason":     reason,
            },
        )
        self._last_action_at = time.monotonic()
        try:
            handle_engine_start(self.db, self.unit_id, {})
        except Exception as e:
            self._log_event(
                "engine.auto_start_failed",
                f"Auto-start raised: {e}",
                "warning",
                {"soc": soc, "reason": reason, "error": str(e)},
            )
            print(f"[supervisor] auto_start failed: {e!r}", flush=True)
            return

        # Did it catch?
        state = self._read_engine_state()
        if state != "running":
            # start macro publishes the failure state itself; we just log
            # that the catch never happened so the event feed has both signals.
            self._log_event(
                "engine.auto_start_no_catch",
                f"Engine.start completed but state={state!r} (no catch)",
                "warning",
                {"endState": state, "reason": reason},
            )
            return

        # Engine running — kick off the charge immediately.
        try:
            handle_engine_charge(self.db, self.unit_id, {})
            self._log_event(
                "engine.auto_charge",
                f"Auto-charging after start (soc={soc}%, target voltageStop={self._load_voltage_stop():.1f}V)",
                "info",
                {"soc": soc, "voltageStop": self._load_voltage_stop()},
            )
        except Exception as e:
            self._log_event(
                "engine.auto_charge_failed",
                f"Auto-charge raised: {e}",
                "warning",
                {"soc": soc, "error": str(e)},
            )
            print(f"[supervisor] auto_charge after start failed: {e!r}", flush=True)

    def _auto_charge(self, soc: int, soc_source: str) -> None:
        """Engine is already running unloaded → load it."""
        from engine import handle_engine_charge

        self._last_action_at = time.monotonic()
        try:
            handle_engine_charge(self.db, self.unit_id, {})
            self._log_event(
                "engine.auto_charge",
                f"Auto-charging (soc={soc}% via {soc_source}; target voltageStop={self._load_voltage_stop():.1f}V)",
                "info",
                {"soc": soc, "socSource": soc_source, "voltageStop": self._load_voltage_stop()},
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
                {"soc": soc, "error": msg},
            )
            print(f"[supervisor] auto_charge failed: {e!r}", flush=True)

    def _auto_stop(self, soc: int, soc_source: str, ttempty: Optional[int]) -> None:
        from engine import handle_engine_stop

        self._last_action_at = time.monotonic()
        msg_runway = f", runway={ttempty}m" if ttempty is not None else ", no load"
        try:
            self._log_event(
                "engine.auto_stop",
                f"Auto-stopping (soc={soc}% via {soc_source}{msg_runway})",
                "info",
                {"soc": soc, "socSource": soc_source, "ttempty": ttempty},
            )
            handle_engine_stop(self.db, self.unit_id, {})
        except Exception as e:
            self._log_event(
                "engine.auto_stop_failed",
                f"Auto-stop raised: {e}",
                "warning",
                {"soc": soc, "error": str(e)},
            )
            print(f"[supervisor] auto_stop failed: {e!r}", flush=True)

    def _note_keep_running(self, soc: int, ttempty: Optional[int], sustainable: int) -> None:
        """SoC is at/above stop threshold but load is too heavy to stop —
        we'd just have to immediately restart. Log once per cooldown so
        the user sees we're holding off intentionally. Doesn't trigger
        the action cooldown (no command was issued)."""
        # Throttle: only log if we haven't logged a keep-running event recently.
        # Use _last_keep_running_at, separate from _last_action_at.
        now = time.monotonic()
        if (now - getattr(self, "_last_keep_running_at", 0.0)) < 300.0:
            return
        self._last_keep_running_at = now
        self._log_event(
            "engine.keep_running",
            f"Holding engine running: soc={soc}% (stop threshold met) but "
            f"runway={ttempty}m < sustainableRunwayMin={sustainable}m — "
            f"load is heavy, stopping would force immediate restart.",
            "info",
            {"soc": soc, "ttempty": ttempty, "sustainableRunwayMin": sustainable},
        )

    # ── readers ───────────────────────────────────────────────────────────

    def _load_supervisor_config(self) -> Dict[str, Any]:
        cfg = dict(DEFAULT_SUPERVISOR_CONFIG)
        # Phase G.5 sub-config (voltages, intervals, etc.) lives here.
        try:
            snap = self.db.document(f"units/{self.unit_id}/config/engine").get()
            if snap.exists:
                block = (snap.to_dict() or {}).get("supervisor", {})
                cfg.update(block)
        except Exception as e:
            print(f"[supervisor] config/engine read failed: {e!r}", flush=True)

        # The user-facing "Auto Recharge" toggle on the Schedule tab writes
        # to config/charge.enabled (carried over from the legacy Phase C
        # scheduler). It is the single source of truth for the supervisor:
        # when config/charge exists, its `enabled` value fully determines
        # whether the supervisor acts, overriding config/engine.supervisor.enabled
        # in BOTH directions. (config/engine.supervisor.enabled only survives
        # as a fallback when config/charge is missing entirely.)
        try:
            charge_snap = self.db.document(f"units/{self.unit_id}/config/charge").get()
            if charge_snap.exists:
                cfg["enabled"] = bool((charge_snap.to_dict() or {}).get("enabled", False))
        except Exception as e:
            print(f"[supervisor] config/charge read failed: {e!r}", flush=True)

        return cfg

    def _load_voltage_stop(self) -> float:
        # 15S LiFePO4 default — used only for logging / event metadata
        # since the supervisor's decisions are SoC-based now. Charge loop
        # uses its own config-driven value.
        try:
            snap = self.db.document(f"units/{self.unit_id}/config/engine").get()
            if snap.exists:
                charge_block = (snap.to_dict() or {}).get("charge", {})
                return float(charge_block.get("voltageStop", 52.5))
        except Exception:
            pass
        return 52.5

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

    def _read_engine_telemetry(self) -> Tuple[Optional[str], Dict[str, Any]]:
        """Read engine state + snapshot fields the supervisor needs.

        Returns (state, telem) where telem has keys:
          battery_soc      Optional[int]    Predator LCD coulomb-counted SoC
          ttempty          Optional[int]    Predator time_to_empty_minutes
          output_watts     Optional[int]    Predator inverter output watts
          motor_volts      Optional[float]  VESC pack voltage (fallback signal)
        """
        state: Optional[str] = None
        telem: Dict[str, Any] = {
            "battery_soc":  None,
            "ttempty":      None,
            "output_watts": None,
            "motor_volts":  None,
        }
        try:
            engine_snap = self.db.document(f"units/{self.unit_id}/current/engine").get()
            if engine_snap.exists:
                state = (engine_snap.to_dict() or {}).get("state")
        except Exception:
            pass
        try:
            snap = self.db.document(f"units/{self.unit_id}/current/snapshot").get()
            if snap.exists:
                d = snap.to_dict() or {}
                soc = d.get("battery_soc")
                if isinstance(soc, (int, float)) and 0 <= soc <= 100:
                    telem["battery_soc"] = int(soc)
                tte = d.get("time_to_empty_minutes")
                if isinstance(tte, (int, float)) and tte >= 0:
                    telem["ttempty"] = int(tte)
                ow = d.get("output_watts")
                if isinstance(ow, (int, float)) and ow >= 0:
                    telem["output_watts"] = int(ow)
                v = d.get("motor_volts")
                if isinstance(v, (int, float)):
                    telem["motor_volts"] = float(v)
        except Exception:
            pass
        return state, telem

    def _read_engine_state(self) -> Optional[str]:
        """Lightweight read for the post-start catch check."""
        try:
            engine_snap = self.db.document(f"units/{self.unit_id}/current/engine").get()
            if engine_snap.exists:
                return (engine_snap.to_dict() or {}).get("state")
        except Exception:
            pass
        return None

    def _resolve_soc(self, telem: Dict[str, Any]) -> Tuple[Optional[int], Optional[str]]:
        """Pick the best SoC source.

        Predator LCD-derived `battery_soc` is the primary because the
        Predator BMS coulomb-counts internally — load IR sag doesn't fool
        it. Falls back to VESC voltage interpreted on the 15S LiFePO4
        curve when battery_soc is null (LCD asleep, or servo wake failed).

        Returns (soc_pct, source_label) or (None, None) if no signal.
        """
        soc = telem.get("battery_soc")
        if soc is not None:
            return int(soc), "predator"
        v = telem.get("motor_volts")
        if v is not None:
            return _volts_to_soc_15s(float(v)), "vesc_voltage"
        return None, None

    def _pre_wake_lcd(self) -> None:
        """Press the Predator LCD wake button right before reading telemetry,
        so battery_soc and time_to_empty are guaranteed fresh.

        Best-effort. If the servo or any dependency fails (e.g. tests on a
        Mac without hardware), swallow and move on — the periodic lcd-wake
        loop is a separate safety net, and the voltage fallback will catch
        the supervisor's decisions if both fail.
        """
        try:
            from servos import wake_lcd  # lazy import (Pi-only deps)
            wake_lcd(self.db, self.unit_id)
            # Give the BMS + I²C sniffer + publisher a moment to land
            # fresh frames into the snapshot.
            time.sleep(1.0)
        except Exception as e:
            # Throttle: log once per 10 minutes so this can't spam if the
            # servo is broken.
            now = time.monotonic()
            if (now - getattr(self, "_last_lcd_wake_warn_at", 0.0)) > 600.0:
                self._last_lcd_wake_warn_at = now
                print(f"[supervisor] LCD pre-wake failed (best effort): {e!r}", flush=True)

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
