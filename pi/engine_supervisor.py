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
  state == failed_engine_bogged, within
    bogRetryWindowSec of our own auto-start,
    engine confirmed stopped, retry budget left      → re-crank ("false catch")
  state startswith "failed" (anything else)         → no-op (manual review)
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

Cold-start crank retry
----------------------
A cold engine routinely needs two cranks to fire. One crank is a
maxDurationSec (~4 s) burst; if it ends in failed_no_catch / failed_no_load
the supervisor waits crankRetryDelaySec and cranks again, up to
crankRetryMax extra attempts, all inside the same _auto_start_cycle call
(so the retry lands well within 30 s of the first miss — the tick interval
and action cooldown never get a chance to delay it).

The retry budget is shared with the "false catch" path: the crank loop can
declare "running" on an engine that fired for a moment and died, in which
case the charge loop is what eventually notices (failed_engine_bogged once
its rampUpSec safety window arms). If that happens within bogRetryWindowSec
of our own auto-start, the engine is confirmed stopped (snapshot motor_rpm
at/below stop.rpmIdleThreshold) and budget remains, the supervisor re-cranks
on the next tick — cooldown is bypassed for this one case. Failures the
supervisor didn't cause (a manual start that missed) are never retried, and
once the budget is spent the failed state is left for manual review exactly
as before.

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

import voltage_soc


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
    # the snapshot. PACK volts, so they only mean anything alongside a cell
    # count — 3.200 and 3.100 V/cell on the fleet's 14S packs. Derive them
    # with voltage_soc.pack_threshold() rather than typing numbers; a pack
    # voltage written without its cell count is the bug that left UNIT-002
    # unable to ever finish a charge.
    "voltageStart":          44.8,
    "voltageCritical":       43.4,

    # How often the supervisor evaluates state. Each tick wakes the LCD
    # before reading, so don't tick too aggressively or the servo wears
    # out. 15-30s is plenty.
    "tickIntervalSec":       15,
    # After firing any auto-action, ignore further triggers for this long.
    "actionCooldownSec":     60,

    # Cold-start crank retry. A cold engine typically needs two cranks to
    # fire, so a single failed_no_catch is not a fault — re-crank after a
    # short starter-cooling pause. crankRetryMax is the number of EXTRA
    # attempts after the first (2 → three cranks total). Budget is per
    # auto-start cycle and is shared with the false-catch path below.
    "crankRetryMax":         2,
    "crankRetryDelaySec":    10,
    # False-catch window. If the charge loop reports failed_engine_bogged
    # within this many seconds of our auto-start, treat it as an engine
    # that fired briefly and died, and re-crank (engine must read stopped
    # first). Must cover engine.charge.rampUpSec, since the charge loop's
    # bog check only arms after the ramp.
    "bogRetryWindowSec":     120,
    # Honor config/charge.quietHours when deciding whether to start.
    # socCritical override still applies if allowQuietOverride=true.
    "respectQuietHours":     True,
}


# The voltage→SoC curve used to live here as a hardcoded pack-volt table
# for a pack wrongly believed to be 15S, with a comment asking whoever
# edited it to also hand-edit the copy in src/lib/voltageSoc.ts. They
# drifted anyway — the two disagreed by 10 points at 49.5 V — and the
# pack-volt form produced nonsense on the real 14S packs. Both problems are
# structural, so the curve now lives once, per cell, in voltage_soc.py.


# Crank outcomes that mean "turned over, didn't fire" — a cold engine, not a
# fault. Anything else (failed_error, failed_low_voltage, …) is left alone.
RETRYABLE_CRANK_STATES = ("failed_no_catch", "failed_no_load")


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
        # Crank-retry bookkeeping for the current auto-start cycle. Reset
        # every time a fresh (non-retry) auto-start fires from idle.
        self._auto_start_at: Optional[float] = None   # monotonic, last attempt
        self._retries_used: int = 0
        self._last_retry_guard_warn_at: float = float("-inf")
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
        # A re-crank after a false catch is the one action allowed through
        # the cooldown: the cooldown exists to stop us thrashing on
        # thresholds, and this isn't a threshold decision — it's finishing
        # the start we already committed to. Cheap when nothing is pending.
        if self._maybe_retry_after_bog(cfg):
            return
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
        # Failure states need manual review — supervisor doesn't auto-recover
        # (the one exception, a false catch right after our own auto-start,
        # was handled by _maybe_retry_after_bog above).
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

        # Never fire the engine to recharge a pack that is already recharging
        # from the wall.  The Predator reports this directly now (reg 0x07
        # bits 2+5); before it was decodable, a charging unit looked like it
        # was discharging on AC, which is the worst possible reading here —
        # it would start the generator next to a working shore-power hookup.
        #
        # Defaults to False when the key is missing so an older snapshot, or
        # one published by a Pi that has not taken this decoder update yet,
        # keeps the previous behaviour rather than silently never starting.
        if telem.get("charging") and state in (None, "idle"):
            self._log_skip_charging(soc, soc_source)
            return

        if state in (None, "idle"):
            # 1. Critical SoC — fires even in quiet hours if user allowed it.
            if soc <= soc_critical and (not in_quiet or allow_quiet_override):
                self._auto_start_cycle(soc, soc_source, ttempty, "critical_soc", cfg)
                return
            # 2. Normal low-SoC trigger.
            if soc <= soc_start and not in_quiet:
                self._auto_start_cycle(soc, soc_source, ttempty, "low_soc", cfg)
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
                self._auto_start_cycle(soc, soc_source, ttempty, "load_aware", cfg)
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
        cfg: Dict[str, Any],
        is_retry: bool = False,
    ) -> None:
        """engine.start (synchronous, re-cranked on a miss) → if caught,
        engine.charge (async). Single-action with a chained charge so we're
        not waiting an entire tick interval to begin loading the engine.

        `reason` is one of: critical_soc, low_soc, load_aware, or
        retry_after_bog. Goes into the event feed so the user can see why
        each auto-start fired. `is_retry` keeps the cycle's retry budget
        instead of resetting it.
        """
        # Import lazily so this module imports cleanly on a Mac without
        # the Pi hardware deps.
        from engine import handle_engine_start, handle_engine_charge

        max_retries = max(0, int(cfg.get("crankRetryMax", 2)))
        retry_delay = max(0.0, float(cfg.get("crankRetryDelaySec", 10)))

        if not is_retry:
            self._retries_used = 0

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
                "attempt":    self._retries_used + 1,
            },
        )

        while True:
            attempt = self._retries_used + 1
            self._last_action_at = time.monotonic()
            self._auto_start_at  = self._last_action_at
            try:
                handle_engine_start(self.db, self.unit_id, {})
            except Exception as e:
                self._log_event(
                    "engine.auto_start_failed",
                    f"Auto-start raised: {e}",
                    "warning",
                    {"soc": soc, "reason": reason, "attempt": attempt, "error": str(e)},
                )
                print(f"[supervisor] auto_start failed: {e!r}", flush=True)
                return

            # Did it catch?
            state = self._read_engine_state()
            if state == "running":
                break

            if state in RETRYABLE_CRANK_STATES and self._retries_used < max_retries:
                # Cold engine, no fire yet — this is the normal two-crank
                # cold start, not a fault. Let the starter rest, then go again.
                self._retries_used += 1
                self._log_event(
                    "engine.auto_start_retry",
                    f"Crank {attempt} ended {state}; re-cranking in {retry_delay:.0f}s "
                    f"(retry {self._retries_used} of {max_retries})",
                    "info",
                    {
                        "endState":    state,
                        "attempt":     attempt,
                        "retry":       self._retries_used,
                        "maxRetries":  max_retries,
                        "delaySec":    retry_delay,
                        "reason":      reason,
                    },
                )
                print(
                    f"[supervisor] crank {attempt} → {state}; retry {self._retries_used}/{max_retries} "
                    f"in {retry_delay:.0f}s",
                    flush=True,
                )
                if self._stop_event.wait(retry_delay):
                    return
                continue

            # start macro publishes the failure state itself; we just log
            # that the catch never happened so the event feed has both signals.
            exhausted = state in RETRYABLE_CRANK_STATES
            self._log_event(
                "engine.auto_start_no_catch",
                f"Engine.start completed but state={state!r} (no catch) after {attempt} "
                f"crank{'s' if attempt != 1 else ''}"
                + ("; retry budget exhausted, leaving for manual review" if exhausted else ""),
                "warning",
                {
                    "endState":   state,
                    "reason":     reason,
                    "attempts":   attempt,
                    "exhausted":  exhausted,
                },
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

    def _maybe_retry_after_bog(self, cfg: Dict[str, Any]) -> bool:
        """False-catch recovery. The crank loop can report "running" for an
        engine that fired for a second and died; the charge loop then finds
        it via failed_engine_bogged once its ramp-in safety window arms.
        If that lands within bogRetryWindowSec of OUR auto-start, the engine
        reads stopped, and retry budget remains, re-crank.

        Returns True if a retry was issued (caller should end the tick).
        Deliberately conservative: unknown RPM → no crank. Cranking a
        genuinely stopped engine is what a human would do next; cranking
        one that is still spinning is not.
        """
        if self._auto_start_at is None:
            return False
        window      = max(0.0, float(cfg.get("bogRetryWindowSec", 120)))
        max_retries = max(0, int(cfg.get("crankRetryMax", 2)))
        elapsed     = time.monotonic() - self._auto_start_at
        if elapsed > window or self._retries_used >= max_retries:
            return False

        state, telem = self._read_engine_telemetry()
        if state != "failed_engine_bogged":
            return False

        rpm = telem.get("motor_rpm")
        stopped_rpm = self._load_stopped_rpm()
        if rpm is None or rpm > stopped_rpm:
            now = time.monotonic()
            if now - self._last_retry_guard_warn_at > 60.0:
                self._last_retry_guard_warn_at = now
                self._log_event(
                    "engine.auto_start_retry_skipped",
                    f"Engine bogged {elapsed:.0f}s after auto-start but motor_rpm={rpm!r} "
                    f"(need ≤{stopped_rpm} to confirm stopped); not re-cranking",
                    "warning",
                    {"motorRpm": rpm, "stoppedRpm": stopped_rpm, "sinceStartSec": round(elapsed, 1)},
                )
            return False

        self._retries_used += 1
        self._log_event(
            "engine.auto_start_retry",
            f"Engine bogged and stopped {elapsed:.0f}s after auto-start (false catch); "
            f"re-cranking (retry {self._retries_used} of {max_retries})",
            "info",
            {
                "endState":      state,
                "retry":         self._retries_used,
                "maxRetries":    max_retries,
                "sinceStartSec": round(elapsed, 1),
                "motorRpm":      rpm,
                "reason":        "retry_after_bog",
            },
        )
        print(
            f"[supervisor] false catch (bogged {elapsed:.0f}s after start, rpm={rpm}); "
            f"retry {self._retries_used}/{max_retries}",
            flush=True,
        )
        soc, soc_source = self._resolve_soc(telem)
        self._auto_start_cycle(
            soc if soc is not None else -1,
            soc_source or "unknown",
            telem.get("ttempty"),
            "retry_after_bog",
            cfg,
            is_retry=True,
        )
        return True

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

    def _log_skip_charging(self, soc: int, soc_source: str) -> None:
        """The pack is on the wall charger, so every auto-start trigger is
        suppressed.  Throttled like _note_keep_running: a unit left plugged
        in overnight would otherwise write an event every tick."""
        now = time.monotonic()
        if (now - getattr(self, "_last_skip_charging_at", 0.0)) < 300.0:
            return
        self._last_skip_charging_at = now
        self._log_event(
            "engine.skip_charging",
            f"Suppressing auto-start: soc={soc}% ({soc_source}) but the "
            f"Predator is charging from the wall — shore power is already "
            f"refilling the pack.",
            "info",
            {"soc": soc, "socSource": soc_source},
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
        # Used only for logging / event metadata since the supervisor's
        # decisions are SoC-based now; the charge loop uses its own
        # config-driven value. Default is 3.500 V/cell on the fleet's 14S.
        try:
            snap = self.db.document(f"units/{self.unit_id}/config/engine").get()
            if snap.exists:
                charge_block = (snap.to_dict() or {}).get("charge", {})
                return float(charge_block.get("voltageStop", 49.0))
        except Exception:
            pass
        return 49.0

    def _load_stopped_rpm(self) -> int:
        """RPM at/below which the engine counts as stopped. Reuses
        engine.stop.rpmIdleThreshold so "stopped" means the same thing to
        the stop primitive and to the false-catch retry guard."""
        try:
            snap = self.db.document(f"units/{self.unit_id}/config/engine").get()
            if snap.exists:
                stop_block = (snap.to_dict() or {}).get("stop", {})
                raw = stop_block.get("rpmIdleThreshold", 100)
                if isinstance(raw, (int, float)) and raw >= 0:
                    return int(raw)
        except Exception:
            pass
        return 100

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
          motor_rpm        Optional[int]    VESC ERPM — "is the engine actually turning"
        """
        state: Optional[str] = None
        telem: Dict[str, Any] = {
            "battery_soc":  None,
            "ttempty":      None,
            "output_watts": None,
            "motor_volts":  None,
            "motor_rpm":    None,
            "charging":     False,
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
                rpm = d.get("motor_rpm")
                if isinstance(rpm, (int, float)):
                    telem["motor_rpm"] = abs(int(rpm))
                telem["charging"] = d.get("charging") is True
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
        it. Falls back to VESC voltage on this unit's per-cell LiFePO4
        curve when battery_soc is null (LCD asleep, or servo wake failed).

        The fallback compensates for internal-resistance offset using
        `motor_amps_in` when it's available, so a decision made mid-charge
        isn't skewed by the terminal-voltage lift from regen current.

        Returns (soc_pct, source_label) or (None, None) if no signal.
        """
        soc = telem.get("battery_soc")
        if soc is not None:
            return int(soc), "predator"
        v = telem.get("motor_volts")
        if v is not None:
            est = voltage_soc.volts_to_soc(
                float(v),
                cell_count=self._load_cell_count(),
                amps_in=telem.get("motor_amps_in"),
            )
            if est is not None:
                return est, "vesc_voltage"
        return None, None

    def _load_cell_count(self) -> int:
        """Series cell count for this unit's pack, from config/engine.

        Cannot be inferred from voltage — 49 V is a full 14S pack or a
        mid-charge 15S pack, and guessing wrong yields a plausible, wrong
        SoC rather than an error — the whole fleet is 14S. Cached after the
        first successful read;
        a pack's cell count doesn't change without someone rebuilding it.
        """
        cached = getattr(self, "_cell_count_cache", None)
        if cached is not None:
            return cached
        n = voltage_soc.DEFAULT_CELL_COUNT
        try:
            snap = self.db.document(f"units/{self.unit_id}/config/engine").get()
            if snap.exists:
                raw = (snap.to_dict() or {}).get("cellCount")
                if isinstance(raw, (int, float)) and 4 <= int(raw) <= 24:
                    n = int(raw)
                else:
                    print(
                        f"[supervisor] config/engine.cellCount missing or "
                        f"invalid ({raw!r}); assuming {n}S. A wrong cell "
                        f"count silently distorts every voltage-derived SoC.",
                        flush=True,
                    )
        except Exception as e:
            print(f"[supervisor] cellCount read failed: {e!r}", flush=True)
        self._cell_count_cache = n
        print(f"[supervisor] pack {voltage_soc.describe(n)}", flush=True)
        return n

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
