"""
Bench crank trigger — single current pulse, no engine.py state machine.

For verifying Pi → CAN → VESC end-to-end with the motor on the bench
(no engine, no fuel, no choke servos). Sends SET_CURRENT at a fixed
amperage for a fixed duration, streams live RPM / duty / temp from the
listener so you can see the motor respond, and releases the motor at
the end (or on Ctrl-C).

Examples
--------
    # Default: 30 A for 1.5 s (gentle first test)
    python3 -u pi/bench_crank.py

    # Real crank-current pulse (matches seed_config default)
    python3 -u pi/bench_crank.py --amps 65 --duration 2.0

    # With pole-pair conversion so logged "rpm" is mechanical
    python3 -u pi/bench_crank.py --amps 30 --pole-pairs 14

Before running on bench
-----------------------
Stop any Pi services that touch the VESC over CAN, otherwise their
SET_CURRENT(0) frames will fight with this script:

    sudo systemctl stop sitepulse-engine-supervisor.service
    sudo systemctl stop sitepulse-command-listener.service

The VESC's own 1 s command-timeout watchdog is your final backstop —
if this script crashes the motor stops within 1 s regardless.

Env overrides
-------------
    SITEPULSE_VESC_IFACE     socketcan interface, default 'can0'
    SITEPULSE_VESC_UNIT_ID   VESC unit ID (set to 100 to match live)
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vesc_sender import VescSender, VESC_IFACE, VESC_UNIT_ID  # noqa: E402
from vesc_listener import VescListener  # noqa: E402


# Hard ceilings independent of VESC's own current limits. The script
# refuses to command above these even if --amps says otherwise.
MAX_AMPS         = 80.0
MAX_DURATION_SEC = 5.0
COMMAND_HZ       = 10   # > 5 Hz needed for VESC command-timeout watchdog
PRINT_HZ         = 5    # how often to log telemetry to stdout


_global_sender: Optional[VescSender] = None


def _release_on_signal(signum, _frame):  # type: ignore[no-untyped-def]
    print(f"\n[signal {signum}] releasing motor", flush=True)
    try:
        if _global_sender is not None:
            _global_sender.release()
    finally:
        sys.exit(130)


def main() -> None:
    ap = argparse.ArgumentParser(description="single-pulse bench crank trigger")
    ap.add_argument("--amps",        type=float, default=30.0, help=f"motor current to command (clamped to {MAX_AMPS})")
    ap.add_argument("--duration",    type=float, default=1.5,  help=f"hold time in seconds (clamped to {MAX_DURATION_SEC})")
    ap.add_argument("--pole-pairs",  type=int,   default=None, help="pole pairs (14 for the 28-magnet motor) — converts logged RPM to mechanical")
    ap.add_argument("--iface",       default=None,             help="socketcan iface (default: env SITEPULSE_VESC_IFACE or can0)")
    ap.add_argument("--unit-id",     type=int, default=None,   help="VESC unit ID (default: env or 0; set to 100 to match live)")
    args = ap.parse_args()

    amps = max(0.0, min(MAX_AMPS, args.amps))
    duration = max(0.1, min(MAX_DURATION_SEC, args.duration))
    if amps != args.amps:
        print(f"[clamp] amps {args.amps} → {amps} (hard ceiling)", flush=True)
    if duration != args.duration:
        print(f"[clamp] duration {args.duration} → {duration} s", flush=True)

    iface = args.iface or VESC_IFACE
    unit  = args.unit_id if args.unit_id is not None else VESC_UNIT_ID

    signal.signal(signal.SIGINT,  _release_on_signal)
    signal.signal(signal.SIGTERM, _release_on_signal)

    sender = VescSender(iface=iface, unit_id=unit)
    listener = VescListener(iface=iface, unit_id=unit)

    global _global_sender
    sender.open()
    _global_sender = sender
    listener.start()

    # Wait briefly so the first listener snapshot has data.
    time.sleep(0.4)
    pre = listener.snapshot()
    if pre.get("motor_stale", True):
        print(
            "[warn] no STATUS frames received yet — check sitepulse-can.service "
            "and that the VESC is powered.",
            flush=True,
        )
    else:
        print(
            f"[ok] VESC online   V={pre.get('motor_volts')}  "
            f"eRPM={pre.get('motor_rpm')}  duty={pre.get('motor_duty')}",
            flush=True,
        )

    print(f"[run] sending SET_CURRENT({amps:+.1f} A) for {duration:.2f} s on {iface} (unit {unit})", flush=True)

    cmd_interval   = 1.0 / COMMAND_HZ
    print_interval = 1.0 / PRINT_HZ

    started   = time.monotonic()
    end       = started + duration
    next_cmd  = started
    next_log  = started
    peak_rpm  = 0
    peak_amps = 0.0
    peak_fet  = 0.0

    try:
        while True:
            now = time.monotonic()
            if now >= end:
                break

            if now >= next_cmd:
                sender.set_current(amps)
                next_cmd = now + cmd_interval

            if now >= next_log:
                snap = listener.snapshot()
                e_rpm  = snap.get("motor_rpm")
                m_amps = snap.get("motor_amps")
                fet    = snap.get("motor_fet_temp_c")
                volts  = snap.get("motor_volts")

                if e_rpm is not None and e_rpm > peak_rpm:
                    peak_rpm = e_rpm
                if m_amps is not None and abs(m_amps) > peak_amps:
                    peak_amps = abs(m_amps)
                if fet is not None and fet > peak_fet:
                    peak_fet = fet

                if args.pole_pairs and e_rpm is not None:
                    rpm_disp = f"{e_rpm} eRPM ({e_rpm // args.pole_pairs} mech)"
                else:
                    rpm_disp = f"{e_rpm} eRPM"

                print(
                    f"  t={now - started:4.2f}s  {rpm_disp}  "
                    f"motor_A={m_amps}  V={volts}  fet={fet}",
                    flush=True,
                )
                next_log = now + print_interval

            time.sleep(0.005)
    finally:
        sender.release()
        time.sleep(0.05)
        sender.release()  # belt-and-braces
        sender.close()
        listener.stop()

    if args.pole_pairs:
        mech = peak_rpm // args.pole_pairs
        print(
            f"\n[summary] peak eRPM={peak_rpm} ({mech} mech RPM)  "
            f"peak |motor_A|={peak_amps:.1f}  peak fet={peak_fet:.1f} C",
            flush=True,
        )
    else:
        print(
            f"\n[summary] peak eRPM={peak_rpm}  "
            f"peak |motor_A|={peak_amps:.1f}  peak fet={peak_fet:.1f} C",
            flush=True,
        )


if __name__ == "__main__":
    main()
