"""
Bench calibration tool for the VESC + starter-generator motor.

Standalone CLI — separate from engine.py — for empirically deriving the
config values that engine.start.currentAmps and engine.charge.currentAmps
currently guess at.

Subcommands
-----------
  sanity   Low-current positive-current pulses with engine OFF and fuel
           shut off. Verifies CAN comms, motor direction, and that
           STATUS frames arrive. KV sanity-check (rpm / volts under
           light load) is informational only — true no-load KV needs
           the shaft uncoupled, which we can't do once it's bolted to
           the crank.

  crank    Cranking-current sweep. Engine fuel OFF (no fire). Pulses
           positive current from --start-amps to --max-amps in --step
           increments, each pulse --pulse-sec long with a --rest-sec
           release between. Logs peak RPM per pulse. Goal: find the
           minimum current that hits --target-rpm. That's the floor
           for engine.start.currentAmps; production value gets ~20%
           headroom added on top.

  regen    Regen-load sweep. ENGINE RUNNING under spark + throttle.
           Steps negative current from -1 A toward -(--max-amps) in
           --step-amps increments, each step held --hold-sec, ramped
           in over --ramp-sec so the engine doesn't take a step load.
           Logs pack-side current (motor_amps_in) and pack voltage
           every command tick — that product is the actual charge
           power. Bails the step if RPM falls below --min-rpm or
           FET / motor temp trips its ceiling.

Output
------
  CSV at pi/calibration_logs/<subcommand>[_<label>]_<ts>.csv
  Stdout summary printed at the end.

Safety
------
  - Ctrl-C calls sender.release() before exit. Signal handler covers
    SIGINT and SIGTERM. The VESC's own 1 s command-timeout watchdog
    is a second backstop — if this process dies hard the motor stops
    within 1 s regardless.
  - Hard ceilings on commanded current (independent of VESC's own
    motor-current limits, which should also be set conservatively in
    VESC Tool before running this).
  - FET temp > MAX_FET_TEMP_C or motor temp > MAX_MOTOR_TEMP_C aborts
    the current step and exits the sweep.
  - regen aborts a step if RPM drops below the floor (engine bogging
    is the limit, not motor or VESC).

Usage
-----
    # On the Pi, after VESC Tool FOC detection has been run against
    # this specific motor:
    python3 -u pi/calibrate_regen.py sanity
    python3 -u pi/calibrate_regen.py crank --target-rpm 500
    python3 -u pi/calibrate_regen.py regen --throttle-label idle
    python3 -u pi/calibrate_regen.py regen --throttle-label half
    python3 -u pi/calibrate_regen.py regen --throttle-label full

Env overrides
-------------
    SITEPULSE_VESC_IFACE     socketcan interface (default 'can0')
    SITEPULSE_VESC_UNIT_ID   VESC controller unit ID (default 0 — set
                             to 100 to match the live unit per
                             hardware_stack memory)
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional, TextIO, Tuple

# Reuse existing modules — same module path engine.py uses.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vesc_sender import VescSender, VESC_IFACE, VESC_UNIT_ID  # noqa: E402
from vesc_listener import VescListener  # noqa: E402


# ─── safety ceilings ──────────────────────────────────────────────────────
# These are independent of the VESC's own configured motor-current limits.
# VESC Tool's motor-current and battery-current limits should ALSO be set
# conservatively (e.g. 80 A motor, 30 A battery) before running this.

MAX_POSITIVE_CURRENT_A = 80.0    # crank-side ceiling
MAX_REGEN_CURRENT_A    = 30.0    # regen-side ceiling (motor amps, abs)
MAX_FET_TEMP_C         = 70.0    # bail step if exceeded
MAX_MOTOR_TEMP_C       = 90.0    # bail step if exceeded
COMMAND_HZ             = 10      # > 5 Hz needed for VESC command-timeout watchdog

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration_logs")


# ─── helpers ──────────────────────────────────────────────────────────────

def _ts() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _open_csv(subcommand: str, label: Optional[str] = None) -> Tuple[TextIO, "csv._writer", str]:
    os.makedirs(LOG_DIR, exist_ok=True)
    suffix = f"_{label}" if label else ""
    path = os.path.join(LOG_DIR, f"{subcommand}{suffix}_{_ts()}.csv")
    f = open(path, "w", newline="")
    w = csv.writer(f)
    w.writerow([
        "t_s", "phase", "cmd_amps",
        "motor_rpm", "motor_amps", "motor_amps_in",
        "motor_volts", "motor_duty",
        "fet_temp_c", "motor_temp_c",
        "pack_watts",
    ])
    return f, w, path


def _row(t0: float, w, phase: str, cmd_amps: float, snap: Dict[str, Any]) -> Dict[str, Any]:
    v_in = snap.get("motor_amps_in")
    v_v  = snap.get("motor_volts")
    pack_watts = round(v_in * v_v, 1) if (v_in is not None and v_v is not None) else None
    w.writerow([
        round(time.monotonic() - t0, 3),
        phase,
        round(cmd_amps, 2),
        snap.get("motor_rpm"),
        snap.get("motor_amps"),
        snap.get("motor_amps_in"),
        snap.get("motor_volts"),
        snap.get("motor_duty"),
        snap.get("motor_fet_temp_c"),
        snap.get("motor_temp_c"),
        pack_watts,
    ])
    snap["pack_watts"] = pack_watts
    return snap


def _safety_trip(snap: Dict[str, Any], rpm_floor: Optional[int] = None) -> Optional[str]:
    """Return a string reason if a safety threshold is tripped, else None."""
    fet = snap.get("motor_fet_temp_c")
    if fet is not None and fet > MAX_FET_TEMP_C:
        return f"FET temp {fet:.1f} C > {MAX_FET_TEMP_C}"
    mt = snap.get("motor_temp_c")
    if mt is not None and mt > MAX_MOTOR_TEMP_C:
        return f"motor temp {mt:.1f} C > {MAX_MOTOR_TEMP_C}"
    if rpm_floor is not None:
        rpm = snap.get("motor_rpm")
        # Ignore the first sample (rpm might still be 0 from before).
        if rpm is not None and rpm < rpm_floor:
            return f"RPM {rpm} < floor {rpm_floor}"
    return None


def _build_io(args: argparse.Namespace) -> Tuple[VescSender, VescListener]:
    global _global_sender
    iface = args.iface or VESC_IFACE
    unit  = args.unit_id if args.unit_id is not None else VESC_UNIT_ID
    sender = VescSender(iface=iface, unit_id=unit)
    listener = VescListener(iface=iface, unit_id=unit)
    sender.open()
    _global_sender = sender  # so SIGINT/SIGTERM can release() before exit
    listener.start()
    # Give the listener ~0.5 s to receive a frame so first snapshot isn't empty.
    time.sleep(0.5)
    snap = listener.snapshot()
    if snap.get("motor_stale", True):
        print(
            "[warn] no VESC STATUS frames received yet — check that the "
            "controller is powered and CAN is up (sitepulse-can.service).",
            flush=True,
        )
    else:
        print(
            f"[ok] VESC online: V={snap.get('motor_volts')} "
            f"RPM={snap.get('motor_rpm')} duty={snap.get('motor_duty')}",
            flush=True,
        )
    return sender, listener


# ─── sweep primitive ──────────────────────────────────────────────────────

def _hold_current(
    sender: VescSender,
    listener: VescListener,
    t0: float,
    writer,
    phase: str,
    target_amps: float,
    hold_sec: float,
    rpm_floor: Optional[int] = None,
    ramp_sec: float = 0.0,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Send `target_amps` at COMMAND_HZ for hold_sec seconds, optionally
    ramped in over ramp_sec. Logs every tick. Returns (samples, trip_reason).
    """
    interval = 1.0 / COMMAND_HZ
    started = time.monotonic()
    end = started + hold_sec
    samples: List[Dict[str, Any]] = []
    next_tick = started
    trip_reason: Optional[str] = None

    while True:
        now = time.monotonic()
        if now >= end:
            break
        if now >= next_tick:
            if ramp_sec > 0 and (now - started) < ramp_sec:
                frac = (now - started) / ramp_sec
            else:
                frac = 1.0
            sender.set_current(target_amps * frac)
            next_tick = now + interval

            snap = listener.snapshot()
            samples.append(_row(t0, writer, phase, target_amps * frac, snap))
            # rpm_floor only meaningful past ramp window
            if (now - started) >= ramp_sec:
                trip = _safety_trip(snap, rpm_floor=rpm_floor)
                if trip is not None:
                    trip_reason = trip
                    break
        time.sleep(0.005)

    return samples, trip_reason


# ─── subcommand: sanity ───────────────────────────────────────────────────

def cmd_sanity(args: argparse.Namespace) -> None:
    print("=== SANITY ===  engine OFF, fuel OFF, motor uncoupled if possible")
    print("Pulses: 1 A, 2 A, 5 A for 2 s each.")
    print()

    sender, listener = _build_io(args)
    f, writer, path = _open_csv("sanity")
    t0 = time.monotonic()
    try:
        for amps in [1.0, 2.0, 5.0]:
            print(f"[step] +{amps} A for 2.0 s")
            samples, trip = _hold_current(sender, listener, t0, writer, f"pulse_{amps:.0f}A", amps, 2.0)
            sender.release()
            time.sleep(1.0)
            if trip:
                print(f"  ABORT: {trip}")
                break
            if samples:
                peaks = max((s.get("motor_rpm") or 0) for s in samples)
                volts = max((s.get("motor_volts") or 0) for s in samples)
                print(f"  peak RPM={peaks}  V={volts}")
        print(f"\nlog: {path}")
    finally:
        sender.release()
        sender.close()
        listener.stop()
        f.close()


# ─── subcommand: crank ────────────────────────────────────────────────────

def cmd_crank(args: argparse.Namespace) -> None:
    print("=== CRANK ===  FUEL VALVE OFF.  IGNITION OFF.")
    print(f"Sweep: {args.start_amps} → {args.max_amps} A in {args.step} A steps")
    print(f"Pulse {args.pulse_sec} s, rest {args.rest_sec} s, target RPM={args.target_rpm}")
    print()
    confirm = input("Fuel valve confirmed OFF? [yes/N] ").strip().lower()
    if confirm != "yes":
        print("aborted")
        return

    sender, listener = _build_io(args)
    f, writer, path = _open_csv("crank")
    t0 = time.monotonic()
    results: List[Tuple[float, int]] = []  # (amps, peak_rpm)

    try:
        amps = args.start_amps
        while amps <= args.max_amps + 0.01 and amps <= MAX_POSITIVE_CURRENT_A:
            print(f"[step] +{amps:.0f} A for {args.pulse_sec} s")
            samples, trip = _hold_current(
                sender, listener, t0, writer,
                phase=f"crank_{amps:.0f}A",
                target_amps=amps,
                hold_sec=args.pulse_sec,
            )
            sender.release()
            if trip:
                print(f"  ABORT: {trip}")
                break

            peak = max((s.get("motor_rpm") or 0) for s in samples) if samples else 0
            results.append((amps, peak))
            print(f"  peak RPM={peak}")

            # Done as soon as we comfortably exceed the target.
            if peak >= args.target_rpm * 1.1:
                print(f"  target RPM exceeded — sweep complete")
                break

            time.sleep(args.rest_sec)
            amps += args.step
    finally:
        sender.release()
        sender.close()
        listener.stop()
        f.close()

    print()
    print("=== summary ===")
    for a, r in results:
        marker = "  <-- meets target" if r >= args.target_rpm else ""
        print(f"  {a:5.1f} A  ->  {r:5d} RPM{marker}")
    hit = next((a for a, r in results if r >= args.target_rpm), None)
    if hit is None:
        print(f"\nNever reached target {args.target_rpm} RPM up to {results[-1][0] if results else 0} A.")
        print("Likely needs: higher VESC motor-current limit, or check coupling/timing.")
    else:
        recommended = hit * 1.2
        print(f"\nMin current to hit {args.target_rpm} RPM: {hit:.1f} A")
        print(f"Recommended engine.start.currentAmps (1.2× headroom): {recommended:.1f} A")
    print(f"\nlog: {path}")


# ─── subcommand: regen ────────────────────────────────────────────────────

def cmd_regen(args: argparse.Namespace) -> None:
    print(f"=== REGEN ===  ENGINE RUNNING at throttle: {args.throttle_label}")
    print(f"Sweep: -1 A → -{args.max_amps} A in {args.step_amps} A steps")
    print(f"Hold {args.hold_sec} s per step, ramp {args.ramp_sec} s, RPM floor={args.min_rpm}")
    print()
    confirm = input("Engine running and stable at requested throttle? [yes/N] ").strip().lower()
    if confirm != "yes":
        print("aborted")
        return

    sender, listener = _build_io(args)
    f, writer, path = _open_csv("regen", label=args.throttle_label)
    t0 = time.monotonic()
    rows: List[Tuple[float, float, float, float, float]] = []
    # (cmd_amps, mean_pack_amps, mean_pack_volts, mean_pack_watts, mean_rpm)
    abort_reason: Optional[str] = None

    try:
        amps = args.step_amps
        while amps <= min(args.max_amps, MAX_REGEN_CURRENT_A) + 0.01:
            target = -amps  # negative = regen / charge
            print(f"[step] {target:+.1f} A for {args.hold_sec} s (ramp {args.ramp_sec} s)")
            samples, trip = _hold_current(
                sender, listener, t0, writer,
                phase=f"regen_{amps:.0f}A",
                target_amps=target,
                hold_sec=args.hold_sec,
                ramp_sec=args.ramp_sec,
                rpm_floor=args.min_rpm,
            )
            # Always release between steps so the engine recovers.
            sender.release()

            # Only score samples past the ramp window for the mean.
            scored = samples[int(args.ramp_sec * COMMAND_HZ):] if samples else []
            if scored:
                mean_amps_in = sum((s.get("motor_amps_in") or 0) for s in scored) / len(scored)
                mean_volts   = sum((s.get("motor_volts") or 0) for s in scored) / len(scored)
                mean_watts   = mean_amps_in * mean_volts
                mean_rpm     = sum((s.get("motor_rpm") or 0) for s in scored) / len(scored)
                rows.append((amps, mean_amps_in, mean_volts, mean_watts, mean_rpm))
                print(
                    f"  pack: {mean_amps_in:5.2f} A  {mean_volts:5.2f} V  "
                    f"{mean_watts:6.1f} W   RPM {mean_rpm:6.0f}"
                )
            if trip:
                abort_reason = trip
                print(f"  ABORT: {trip}")
                break

            time.sleep(args.rest_sec)
            amps += args.step_amps
    finally:
        sender.release()
        sender.close()
        listener.stop()
        f.close()

    print()
    print("=== summary ===")
    print(f"  throttle: {args.throttle_label}")
    if rows:
        print(f"  {'cmd_A':>6}  {'pack_A':>7}  {'pack_V':>7}  {'pack_W':>7}  {'rpm':>6}")
        for a, ia, v, w, r in rows:
            print(f"  {a:6.1f}  {ia:7.2f}  {v:7.2f}  {w:7.1f}  {r:6.0f}")
        # Pick the highest commanded current that didn't trigger an abort and
        # held RPM above the floor as the recommended max for this throttle.
        last = rows[-1]
        if abort_reason is None:
            print(f"\nMax sustainable regen @ {args.throttle_label}: ~{last[0]:.1f} A motor "
                  f"({last[3]:.1f} W into pack)")
        else:
            print(f"\nAborted at {last[0]:.1f} A — last sustainable was the step before.")
    else:
        print("  no samples collected")
    print(f"\nlog: {path}")


# ─── arg parser ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="VESC starter-generator bench calibration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See module docstring for safety prerequisites.",
    )
    p.add_argument("--iface", default=None, help="socketcan iface (default: env SITEPULSE_VESC_IFACE or can0)")
    p.add_argument("--unit-id", type=int, default=None, help="VESC unit ID (default: env or 0)")

    sub = p.add_subparsers(dest="subcommand", required=True)

    sp = sub.add_parser("sanity", help="low-current pulses to verify comms (engine OFF)")
    sp.set_defaults(func=cmd_sanity)

    cp = sub.add_parser("crank", help="cranking-current sweep (fuel OFF)")
    cp.add_argument("--start-amps",  type=float, default=20.0)
    cp.add_argument("--max-amps",    type=float, default=80.0)
    cp.add_argument("--step",        type=float, default=5.0)
    cp.add_argument("--pulse-sec",   type=float, default=0.5)
    cp.add_argument("--rest-sec",    type=float, default=2.0)
    cp.add_argument("--target-rpm",  type=int,   default=500)
    cp.set_defaults(func=cmd_crank)

    rp = sub.add_parser("regen", help="regen-load sweep (engine RUNNING)")
    rp.add_argument("--throttle-label", required=True, choices=["idle", "half", "full"],
                    help="manual throttle position label — set throttle BEFORE running")
    rp.add_argument("--max-amps",  type=float, default=25.0, help="ceiling on |motor amps| (default 25, hard 30)")
    rp.add_argument("--step-amps", type=float, default=1.0)
    rp.add_argument("--hold-sec",  type=float, default=3.0)
    rp.add_argument("--ramp-sec",  type=float, default=0.8)
    rp.add_argument("--rest-sec",  type=float, default=1.5)
    rp.add_argument("--min-rpm",   type=int,   default=1500, help="bail step if RPM falls below this")
    rp.set_defaults(func=cmd_regen)

    return p


# ─── signal-safe release ──────────────────────────────────────────────────

_global_sender: Optional[VescSender] = None


def _release_on_signal(signum, frame):  # type: ignore[no-untyped-def]
    print(f"\n[signal {signum}] releasing motor and exiting", flush=True)
    try:
        if _global_sender is not None:
            _global_sender.release()
    finally:
        sys.exit(130)


def main() -> None:
    signal.signal(signal.SIGINT,  _release_on_signal)
    signal.signal(signal.SIGTERM, _release_on_signal)
    args = _build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
