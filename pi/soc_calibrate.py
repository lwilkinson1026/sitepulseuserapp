"""Rebuild the voltage→SoC curve from real hardware, using the LCD as truth.

Why this exists
───────────────
`voltage_soc.SOC_CURVE_V_PER_CELL` is well anchored at the top (its 100 %
point matches both packs' measured resting-full voltage) but its MIDDLE is
inherited from an older curve and has never been verified against hardware.
Measured 2026-08-04 on UNIT-002: pack at 48.1 V with the LCD reporting
32 %, where the curve claims ~90 %. A 58-point error.

That matters because the curve is a CONTROL input, not just a display: when
the LCD is dark, `scheduler` and `engine_supervisor` fall back to it. A
curve that reads 90 % on a 32 % pack will refuse to start a charge on a
pack that badly needs one — the same failure as the phantom-SoC bug, just
arriving by a different route.

The Predator coulomb-counts internally, so its LCD SoC is immune to the IR
sag that makes voltage unreliable. It is the ground truth here. This script
samples (voltage, LCD SoC) pairs over a full discharge, then fits a
per-cell curve from them.

Usage
─────
    # collect — run for a full discharge cycle (hours). Append-only and
    # resumable; safe to stop and restart.
    python3 soc_calibrate.py log --unit UNIT-002 --cells 14 \\
        --out ~/soc_cal_UNIT-002.csv

    # fit — once you have coverage across the SoC range
    python3 soc_calibrate.py fit --in ~/soc_cal_UNIT-002.csv

Run the collector detached so an SSH drop doesn't kill it:
    nohup python3 soc_calibrate.py log --unit UNIT-002 --cells 14 \\
        --out ~/soc_cal.csv > ~/soc_cal.out 2>&1 &

Getting a curve worth trusting
──────────────────────────────
  * **Span a real discharge.** LiFePO4's plateau means most of the useful
    information is at the ends. A curve fit only from 60-100 % will be
    worse than what it replaces.
  * **Rest matters more than anything.** Terminal voltage under load sags,
    and after load removal it takes minutes to settle. Samples are tagged
    with the current at capture; the fitter defaults to discarding anything
    above --max-amps so the curve describes RESTING voltage.
  * **Do not calibrate during a charge run.** Regen lifts terminal voltage
    the other way. Those samples are recorded but excluded by default.
  * **One curve per chemistry, not per unit.** The output is in V/cell, so
    a curve measured on the 14S unit applies to the 15S one. If the two
    disagree after fitting, that is real information — investigate rather
    than keeping two curves.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from typing import List, Optional, Tuple

CSV_FIELDS = ("iso", "unit", "cells", "volts", "amps", "amps_in",
              "lcd_soc", "v_per_cell", "lcd_awake", "engine_state")


def _db():
    # Imported lazily so `fit` runs anywhere a CSV does — on a laptop with
    # no firebase-admin installed, for instance. Only `log` talks to
    # Firestore, and only `log` has to run on the Pi.
    import firebase_admin
    from firebase_admin import credentials, firestore

    sa = os.environ.get("SITEPULSE_SA")
    if not sa:
        print("set SITEPULSE_SA to the service-account JSON path", file=sys.stderr)
        raise SystemExit(2)
    sa = os.path.expanduser(sa)
    if not os.path.exists(sa):
        print(f"service account not found: {sa}", file=sys.stderr)
        raise SystemExit(2)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(sa))
    return firestore.client()


# ─── collect ───────────────────────────────────────────────────────────────

def cmd_log(args) -> int:
    db = _db()
    snap_ref = db.document(f"units/{args.unit}/current/snapshot")
    eng_ref = db.document(f"units/{args.unit}/current/engine")

    new_file = not os.path.exists(args.out)
    fh = open(args.out, "a", newline="")
    w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
    if new_file:
        w.writeheader()
        fh.flush()

    print(f"logging {args.unit} ({args.cells}S) every {args.interval}s -> {args.out}",
          flush=True)
    print("Ctrl-C to stop; the file is append-only and resumable.\n", flush=True)

    kept = skipped = 0
    last_soc: Optional[int] = None
    try:
        while True:
            try:
                s = snap_ref.get().to_dict() or {}
                e = eng_ref.get().to_dict() or {}
            except Exception as ex:
                print(f"  read failed: {ex!r}", flush=True)
                time.sleep(args.interval)
                continue

            volts = s.get("motor_volts")
            soc = s.get("battery_soc")
            awake = s.get("lcd_awake")

            # A sample is only useful with BOTH sides of the pair. A null SoC
            # means the LCD is dark, which is exactly the condition the curve
            # exists to cover — but it gives us nothing to calibrate against.
            if volts is None or soc is None:
                skipped += 1
                if skipped % 10 == 1:
                    print(f"  skip: volts={volts} lcd_soc={soc} "
                          f"lcd_awake={awake} (need both)", flush=True)
                time.sleep(args.interval)
                continue

            row = {
                "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "unit": args.unit,
                "cells": args.cells,
                "volts": round(float(volts), 3),
                "amps": s.get("motor_amps"),
                "amps_in": s.get("motor_amps_in"),
                "lcd_soc": int(soc),
                "v_per_cell": round(float(volts) / args.cells, 4),
                "lcd_awake": awake,
                "engine_state": e.get("state"),
            }
            w.writerow(row)
            fh.flush()
            kept += 1

            marker = ""
            if last_soc is not None and soc != last_soc:
                marker = f"  <- SoC moved {last_soc} -> {soc}"
            last_soc = int(soc)
            print("  %s  %5.1fV  %.4f V/cell  soc=%3d%%  A=%s%s"
                  % (row["iso"], row["volts"], row["v_per_cell"],
                     row["lcd_soc"], row["amps"], marker), flush=True)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nstopped. {kept} samples written, {skipped} skipped.", flush=True)
    finally:
        fh.close()
    return 0


# ─── fit ───────────────────────────────────────────────────────────────────

def _load(path: str, max_amps: float) -> List[Tuple[int, float]]:
    """Return [(soc, v_per_cell)] for samples taken near rest."""
    out: List[Tuple[int, float]] = []
    dropped_load = 0
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                soc = int(r["lcd_soc"])
                vpc = float(r["v_per_cell"])
                amps = float(r["amps"] or 0.0)
            except (TypeError, ValueError):
                continue
            if abs(amps) > max_amps:
                dropped_load += 1
                continue
            out.append((soc, vpc))
    if dropped_load:
        print(f"  dropped {dropped_load} sample(s) with |amps| > {max_amps} "
              f"(loaded/charging — terminal voltage is not resting voltage)")
    return out


def cmd_fit(args) -> int:
    samples = _load(args.infile, args.max_amps)
    if not samples:
        print("no usable samples", file=sys.stderr)
        return 1

    # Median V/cell per SoC bucket. Median rather than mean so one settling
    # transient can't drag a whole bucket.
    buckets: dict = {}
    for soc, vpc in samples:
        b = min(100, max(0, round(soc / args.bucket) * args.bucket))
        buckets.setdefault(b, []).append(vpc)

    points: List[Tuple[float, int]] = []
    for b in sorted(buckets):
        vals = sorted(buckets[b])
        med = vals[len(vals) // 2]
        points.append((round(med, 4), b))

    print(f"\n{len(samples)} usable samples, {len(points)} buckets "
          f"(width {args.bucket} %)\n")
    print("  SoC%   V/cell   n     pack@14S  pack@15S")
    for vpc, b in points:
        print("  %3d    %.4f   %-4d  %.1f      %.1f"
              % (b, vpc, len(buckets[b]), vpc * 14, vpc * 15))

    # Monotonicity is required by the interpolator. Non-monotonic buckets
    # mean noisy or under-sampled data, not a weird battery.
    bad = [
        (points[i - 1], points[i])
        for i in range(1, len(points))
        if points[i][0] <= points[i - 1][0]
    ]
    if bad:
        print("\n  ⚠️  NON-MONOTONIC buckets — voltage must rise with SoC.")
        for lo, hi in bad:
            print(f"      {lo[1]}%={lo[0]:.4f}  then  {hi[1]}%={hi[0]:.4f}")
        print("      Usually means too few samples there, or samples taken")
        print("      before the pack settled. Collect more before trusting this.")

    lo_cov = min(b for _, b in points)
    hi_cov = max(b for _, b in points)
    print(f"\n  coverage: {lo_cov} % .. {hi_cov} %")
    if lo_cov > 20 or hi_cov < 90:
        print("  ⚠️  Thin coverage at the ends — and the ends are the ONLY")
        print("      region where LiFePO4 voltage carries real information.")
        print("      A curve fit from the plateau alone is worse than none.")

    print("\n─── paste into pi/voltage_soc.py (and mirror in "
          "src/lib/voltageSoc.ts) ───\n")
    print("SOC_CURVE_V_PER_CELL: Sequence[Tuple[float, int]] = (")
    for vpc, b in points:
        print("    (%.4f, %3d)," % (vpc, b))
    print(")")
    print("\nKeep the two files identical — they drifted 10 points apart the")
    print("last time they were maintained by hand.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("log", help="collect (voltage, LCD SoC) samples")
    lg.add_argument("--unit", required=True)
    lg.add_argument("--cells", type=int, required=True,
                    help="series cell count — UNIT-001 is 15, UNIT-002 is 14")
    lg.add_argument("--interval", type=float, default=120.0)
    lg.add_argument("--out", required=True)
    lg.set_defaults(func=cmd_log)

    ft = sub.add_parser("fit", help="fit a per-cell curve from a CSV")
    ft.add_argument("--in", dest="infile", required=True)
    ft.add_argument("--bucket", type=int, default=5, help="SoC bucket width %%")
    ft.add_argument("--max-amps", type=float, default=2.0,
                    help="discard samples above this |current| (default 2.0 A)")
    ft.set_defaults(func=cmd_fit)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
