"""One-time migration: record cellCount on config/engine and derive every
pack-voltage threshold from the shared per-cell curve.

Background
──────────
Every pack-voltage threshold in this system is cell-count specific, but
cell count was never recorded anywhere — it lived in comments, and those
comments said "15S" for a fleet that is actually 14S throughout (confirmed
2026-08-04). UNIT-002's thresholds were rescaled by hand on 2026-07-29 and
happen to be correct. UNIT-001 still carries the original 15S-era numbers.

Current state, measured from Firestore 2026-07-30, against 14S targets:

                    UNIT-001   UNIT-002   14S target   V/cell
  voltageStop         52.5       49.0        49.0      3.500
  voltageStart        48.0       44.8        44.8      3.200
  voltageCritical     46.5       43.4        43.4      3.100
  voltageMinAbort     42.0       42.0        42.0      3.000

What this changes
─────────────────
  UNIT-002: + cellCount=14. No threshold changes — already correct.
  UNIT-001: + cellCount=14, and ALL THREE upper thresholds drop:
              voltageStop     52.5 -> 49.0
              voltageStart    48.0 -> 44.8
              voltageCritical 46.5 -> 43.4
            voltageMinAbort stays 42.0 (already 3.000 V/cell on 14S).

⚠️ The UNIT-001 change is significant and should be sanity-checked against
the hardware before it is trusted in production. Its old 52.5 V
charge-complete is 3.75 V/cell on a 14S pack — above the LiFePO4 ceiling,
therefore unreachable — which means its charge loop could never terminate
on voltage and instead ran to `maxDurationSec` every cycle. That is the
same defect UNIT-002 had before 2026-07-29. After this migration UNIT-001
terminates charging at a reachable voltage for the first time.

Conversely, if UNIT-001's pack ever turns out NOT to be 14S, this
migration makes it stop charging early (49.0 V would be ~58 % on 15S).
The decisive check is a resting-full measurement: ~49.5 V = 14S.

Usage
─────Usage
─────
    python3 migrate_cell_count.py            # dry run, prints a diff
    python3 migrate_cell_count.py --apply    # writes

Idempotent: re-running after a successful apply reports "no changes".
Reads back and verifies every write.
"""

from __future__ import annotations

import os
import sys

import firebase_admin
from firebase_admin import credentials, firestore

import voltage_soc

# Cell count is a physical fact about each pack and is NOT inferable from a
# single voltage reading — 49 V is a full 14S pack or a half-charged 15S
# one. Written down here rather than guessed.
#
# THE FLEET IS 15S (settled 2026-08-04, after two wrong turns).
#
# Both units are the same Predator power station, so a mixed fleet was
# always the surprising answer. The at-rest evidence agrees on both:
#
#   UNIT-001  49.4 V rested (A=0.0), LCD 44 %
#             15S -> 3.29 V/cell ~ 58 %   plausible
#             14S -> 3.53 V/cell ~ FULL   impossible at 44 %
#   UNIT-002  47.8 V rested (A=0.0), LCD 24 %
#             15S -> 3.19 V/cell ~ 20 %   close
#             14S -> 3.41 V/cell ~ 87 %   nowhere near
#
# THE ONE CONTRADICTING DATUM: a 2026-07-29 multimeter reading of 49.5 V
# described as "resting full" on UNIT-002, which implies 14S. That only
# holds if the pack was genuinely full at the time. If it was not, 49.5 V
# on 15S is 3.30 V/cell — an ordinary mid-pack voltage — and the whole 14S
# conclusion collapses. Treat that reading as unconfirmed.
#
# HOW TO PROVE IT EITHER WAY, once and for all: wall-charge a pack to a
# real 100 % on the Predator's own BMS, let it rest, then read pack voltage
# from the VESC. ~52.5-53 V = 15S. ~49.5 V = 14S. Do this before trusting
# any of the above.
#
# The durable lesson: pack voltage ALONE never settles cell count. Pack
# voltage plus a coulomb-counted SoC AT REST does.
FLEET = {
    "UNIT-001": 15,
    "UNIT-002": 15,
}


def desired_engine_patch(cell_count: int) -> dict:
    """The (dotted-path) keys this migration owns, derived per cell."""
    return {
        "cellCount": cell_count,
        "charge.voltageStop": voltage_soc.pack_threshold(
            voltage_soc.STOP_V_PER_CELL, cell_count),
        "charge.voltageMinAbort": voltage_soc.pack_threshold(
            voltage_soc.MIN_ABORT_V_PER_CELL, cell_count),
        "supervisor.voltageStart": voltage_soc.pack_threshold(
            voltage_soc.START_V_PER_CELL, cell_count),
        "supervisor.voltageCritical": voltage_soc.pack_threshold(
            voltage_soc.CRITICAL_V_PER_CELL, cell_count),
    }


def current_value(doc: dict, dotted: str):
    node = doc
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def main() -> int:
    apply = "--apply" in sys.argv

    sa = os.environ.get("SITEPULSE_SA")
    if not sa or not os.path.exists(sa):
        print("set SITEPULSE_SA to the service-account JSON path", file=sys.stderr)
        return 2
    firebase_admin.initialize_app(credentials.Certificate(sa))
    db = firestore.client()

    print("=== cell-count / per-cell threshold migration %s ==="
          % ("(APPLYING)" if apply else "(dry run — nothing will be written)"))

    total_changes = 0
    for unit_id, cells in FLEET.items():
        ref = db.document(f"units/{unit_id}/config/engine")
        snap = ref.get()
        if not snap.exists:
            print(f"\n{unit_id}: config/engine MISSING — skipping (seed it first)")
            continue
        doc = snap.to_dict() or {}

        print(f"\n{unit_id}  {voltage_soc.describe(cells)}")
        patch = desired_engine_patch(cells)
        changes = {}
        for key, want in patch.items():
            have = current_value(doc, key)
            same = (
                have is not None
                and isinstance(have, (int, float))
                and abs(float(have) - float(want)) < 1e-9
            )
            mark = "  ok" if same else "CHANGE"
            print("  %-28s %-8s -> %-8s %s"
                  % (key, have if have is not None else "(unset)", want, mark))
            if not same:
                changes[key] = want

        if not changes:
            print("  (no changes)")
            continue
        total_changes += len(changes)

        if apply:
            ref.update(changes)
            after = ref.get().to_dict() or {}
            bad = [
                k for k, v in patch.items()
                if not (
                    isinstance(current_value(after, k), (int, float))
                    and abs(float(current_value(after, k)) - float(v)) < 1e-9
                )
            ]
            if bad:
                print(f"  !! VERIFY FAILED for {bad}")
                return 1
            print(f"  applied + verified {len(changes)} key(s)")

    print()
    if apply and total_changes:
        # cellCount is cached in-process on first read (a pack's geometry
        # does not change at runtime), so a config write alone does NOT take
        # effect — engine_supervisor and scheduler keep using whatever they
        # read at startup. Learned the hard way 2026-08-04: UNIT-001 logged
        # "pack 14S" and kept using it after the doc had been set to 15.
        print("⚠️  RESTART REQUIRED on every changed unit — cellCount is")
        print("    cached at startup by engine_supervisor and scheduler:")
        print("        sudo systemctl restart sitepulse-listener")
        print("    Confirm with the '[supervisor] pack NNS: ...' line in")
        print("    cmd_listener.log; it echoes the cell count actually in use.")
        print()
    if not apply and total_changes:
        print("Dry run only. Re-run with --apply to write %d change(s)."
              % total_changes)
    elif not total_changes:
        print("Fleet already consistent. Nothing to do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
