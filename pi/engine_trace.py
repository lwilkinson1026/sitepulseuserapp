"""High-rate CAN trace of what the motor is ACTUALLY doing during a start.

Why this exists
───────────────
UNIT-002 loads its engine immediately after cranking, with nothing shown as
charging in the app, and overheats shortly into a charge run. UNIT-001,
running byte-identical code, does not. Every software path that could
command current has been ruled out by inspection:

  * `scheduler._vesc_set_current()` is a STUB — it prints and never sends.
  * `handle_engine_start`'s post-crank path publishes state, sleeps and
    moves the choke. It commands no current.
  * `_do_crank` calls `sender.release()` (SET_CURRENT 0) on catch, and the
    VESC watchdog cuts ~1 s after any last command regardless.
  * `handle_engine_charge` is guarded by `_charge_active` AND `_engine_lock`,
    so two concurrent charge loops cannot exist. No command doubling.

So the question is no longer "what does our code send" but "what is the
motor doing when our code sends nothing" — which needs a measurement, not
more reading. This samples the VESC's own STATUS frames straight off CAN,
independent of the publisher, Firestore, and the engine module.

What to look for
────────────────
  IDLE, no command from us:
    amps ≈ 0, duty ≈ 0            → normal; the VESC is coasting
    amps or duty NON-ZERO         → the VESC is commanding itself. Suspect a
                                     VESC input app (ADC/PPM/UART) enabled
                                     with a floating input. Nothing on the Pi
                                     can cause this.

  DURING CHARGE, comparing the two units at the same commanded amps:
    similar amps, similar temps   → normal
    UNIT-002 draws far more current, or heats much faster, for the same
    commanded regen               → motor/FOC parameters are wrong. Re-run
                                     motor detection in VESC Tool. This unit's
                                     VESC was reflashed (ID 27 → 100); if
                                     detection was not re-run afterward, the
                                     current controller is working off wrong
                                     constants and will draw excess current
                                     and overheat for the same output.

Usage
─────
    python3 engine_trace.py --seconds 120
    python3 engine_trace.py --seconds 300 --csv ~/trace_UNIT-002.csv

Run it, THEN press Start Engine in the app, and let it keep running into a
charge. Reads only — never transmits, so it cannot perturb what it measures.
Safe to run alongside the publisher (both just receive).
"""

from __future__ import annotations

import argparse
import csv
import os
import struct
import sys
import time
from typing import Dict, Optional

CMD_STATUS_1 = 9
CMD_STATUS_4 = 16
CMD_STATUS_5 = 27


def _i32(d: bytes, o: int) -> int:
    return struct.unpack(">i", d[o:o + 4])[0]


def _i16(d: bytes, o: int) -> int:
    return struct.unpack(">h", d[o:o + 2])[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--unit-id", type=int,
                    default=int(os.environ.get("SITEPULSE_VESC_UNIT_ID", "100")),
                    help="VESC controller ID to filter on (default 100)")
    ap.add_argument("--iface", default=os.environ.get("SITEPULSE_VESC_IFACE", "can0"))
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--hz", type=float, default=5.0, help="print rate")
    ap.add_argument("--csv", help="also append rows to this file")
    args = ap.parse_args()

    try:
        import can
    except ImportError:
        print("python-can not installed", file=sys.stderr)
        return 2

    bus = can.interface.Bus(channel=args.iface, interface="socketcan")
    v: Dict[str, float] = {}
    writer = None
    fh = None
    if args.csv:
        new = not os.path.exists(args.csv)
        fh = open(args.csv, "a", newline="")
        writer = csv.writer(fh)
        if new:
            writer.writerow(["t", "rpm", "amps", "duty", "amps_in",
                             "volts", "fet_c", "motor_c", "watts"])

    print(f"tracing VESC id={args.unit_id} on {args.iface} for {args.seconds:.0f}s")
    print("(read-only — this never transmits)\n")
    print("   t     rpm     amps   duty   amps_in  volts  fetC  motC   watts  note")

    t0 = time.monotonic()
    next_print = t0
    interval = 1.0 / max(0.5, args.hz)
    peak_amps = 0.0
    peak_fet = 0.0
    peak_motor = 0.0

    try:
        while time.monotonic() - t0 < args.seconds:
            msg = bus.recv(timeout=0.2)
            now = time.monotonic()
            if msg is not None and msg.is_extended_id:
                cmd = (msg.arbitration_id >> 8) & 0xFF
                uid = msg.arbitration_id & 0xFF
                d = msg.data
                if uid == args.unit_id and len(d) >= 8:
                    if cmd == CMD_STATUS_1:
                        v["rpm"] = _i32(d, 0)
                        v["amps"] = _i16(d, 4) / 10.0
                        v["duty"] = _i16(d, 6) / 1000.0
                    elif cmd == CMD_STATUS_4:
                        v["fet_c"] = _i16(d, 0) / 10.0
                        v["motor_c"] = _i16(d, 2) / 10.0
                        v["amps_in"] = _i16(d, 4) / 10.0
                    elif cmd == CMD_STATUS_5:
                        v["volts"] = _i16(d, 4) / 10.0

            if now < next_print:
                continue
            next_print = now + interval

            amps = v.get("amps", 0.0)
            duty = v.get("duty", 0.0)
            volts = v.get("volts", 0.0)
            amps_in = v.get("amps_in", 0.0)
            watts = volts * amps_in
            peak_amps = max(peak_amps, abs(amps))
            peak_fet = max(peak_fet, v.get("fet_c", 0.0))
            peak_motor = max(peak_motor, v.get("motor_c", 0.0))

            # The line that matters: current flowing with no duty means the
            # controller is doing something on its own.
            note = ""
            if abs(amps) > 1.0 and abs(duty) < 0.01:
                note = "<-- CURRENT WITH ~ZERO DUTY"
            elif abs(amps) > 1.0:
                note = "regen" if amps < 0 else "DRIVE (motoring!)"

            print("%6.1f %7d %7.1f %6.3f %8.1f %6.1f %5.1f %5.1f %7.0f  %s"
                  % (now - t0, v.get("rpm", 0), amps, duty, amps_in, volts,
                     v.get("fet_c", 0.0), v.get("motor_c", 0.0), watts, note),
                  flush=True)
            if writer:
                writer.writerow(["%.2f" % (now - t0), v.get("rpm", 0), amps,
                                 duty, amps_in, volts, v.get("fet_c", 0.0),
                                 v.get("motor_c", 0.0), round(watts)])
                fh.flush()
    except KeyboardInterrupt:
        pass
    finally:
        bus.shutdown()
        if fh:
            fh.close()

    print("\npeak |amps|=%.1f  peak FET=%.1f C  peak motor=%.1f C"
          % (peak_amps, peak_fet, peak_motor))
    print("\nReminder: motor_temp_c is garbage without a probe wired "
          "(UNIT-002 has read -76 C). FET temp is the trustworthy one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
