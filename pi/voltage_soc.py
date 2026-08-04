"""Pack voltage → state-of-charge, defined ONCE, per cell.

THE FLEET IS MIXED: **UNIT-001 is 15S, UNIT-002 is 14S.** Do not "unify"
them. This was briefly set to 14S/14S on 2026-08-04 and reverted the same
day — see "How cell count was settled" below, because the method matters
more than the answer.

Why this module exists
──────────────────────
Every voltage number in this fleet used to be written as an absolute pack
voltage, copied between units by hand. That produced two classes of bug:

  1. **Pack volts written down without the cell count they depend on.**
     A "52.5 V charge-complete" is 3.75 V/cell on 14S — physically
     unreachable for LiFePO4 — so the charge loop could never terminate on
     voltage and instead ran the engine for its full `maxDurationSec`,
     every cycle, silently. That shipped on UNIT-002 and survived until
     2026-07-29, and the same value is still live on UNIT-001 today.

  2. **Two hand-maintained copies of the same curve drifting apart.**
     `engine_supervisor._SOC_CURVE_15S` and `src/lib/voltageSoc.ts` both
     carried a comment saying "keep these in sync". They were not in sync:
     they disagreed by 10 points at 49.5 V. A comment is not a mechanism.

The fix is to express the chemistry ONCE, per cell, and multiply by the
unit's cell count at the point of use. A curve in V/cell is a property of
LiFePO4; a curve in pack volts is a property of one particular pack.

How cell count was settled — the method, because it will come up again
──────────────────────────────────────────────────────────────────────
**Pack voltage alone can never settle cell count.** 49 V is a full 14S
pack or a mid-charge 15S pack, and no amount of staring at telemetry
distinguishes them. That ambiguity produced a year of wrong SoC numbers
and two separate incorrect "corrections".

**Pack voltage plus a coulomb-counted SoC AT REST settles it instantly.**
Measured 2026-08-04:

    UNIT-001  49.4 V, A=0.0 (rested), LCD 44 %
              → 14S = 3.53 V/cell, a FULL pack, cannot read 44 %   ✗
              → 15S = 3.29 V/cell ≈ 58 %, close to the LCD          ✓  15S

    UNIT-002  47.9 V under -23.5 A regen, LCD 55 %
              back out the IR lift: ~45.7 V
              → 14S ≈ 48 %, close to the LCD                        ✓  14S
              → 15S ≈  5 %, nowhere near                            ✗

Note the second one only works AFTER removing the regen voltage lift —
comparing a charging pack's terminal voltage against a resting curve will
mislead you. Rest the pack, or compensate with `amps_in`.

Calibration provenance
──────────────────────
The per-cell shape descends from the curve that used to live in
`src/lib/voltageSoc.ts`, divided by 15 — which is correct, because that
curve was measured on UNIT-001, and UNIT-001 is 15S. Its anchors are
therefore valid and are retained:

    52.8 V resting full on 15S  = 3.520 V/cell  (2026-06-05)
    50.1 V @ LCD 71 % on 15S    = 3.340 V/cell  (2026-06-10)

Both were briefly discarded on 2026-08-04 as "impossible" — but they were
only impossible under the mistaken assumption that UNIT-001 was 14S. On
15S they are ordinary LiFePO4 numbers.

Cross-check on the OTHER pack, which is genuinely 14S: the curve top of
3.5333 V/cell × 14 = 49.5 V matches UNIT-002's measured resting-full
(multimeter, 2026-07-29). So the same per-cell shape is anchored
independently on both packs — which is exactly the property a per-cell
curve is supposed to have.

Accuracy caveat — read before trusting a number from here
─────────────────────────────────────────────────────────
LiFePO4 has a very flat middle plateau: roughly 3.23–3.27 V/cell covers
30 %–50 % SoC. Voltage-derived SoC is therefore only trustworthy near the
extremes. It is a FALLBACK for when the Predator LCD (which coulomb-counts
internally and is immune to this) is unavailable. Anything user-facing
that comes from here should be marked approximate.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

# ─── The curve, in volts per cell ──────────────────────────────────────────
# LiFePO4. Monotonic in both columns; interpolated linearly between points
# and clamped outside them.
SOC_CURVE_V_PER_CELL: Sequence[Tuple[float, int]] = (
    (3.0000,   0),   # BMS cuts well below this (~2.5 V/cell)
    (3.1000,  10),   # ← supervisor.voltageCritical
    (3.2000,  20),   # ← supervisor.voltageStart
    (3.2333,  30),   # ┐
    (3.2667,  50),   # │ the flat plateau — voltage says little here
    (3.3000,  60),   # ┘
    (3.3400,  71),   # inherited; original 15S-era anchor discarded (see header)
    (3.3667,  80),
    (3.4333,  90),
    (3.5000,  95),   # ← engine.charge.voltageStop  (49.0 V on 14S)
    (3.5333, 100),   # resting full — 49.5 V on 14S, measured 2026-07-29
)

# Threshold definitions, also per cell. Multiply by cell count to get the
# pack voltages that live in Firestore config.
STOP_V_PER_CELL          = 3.500   # charge complete
START_V_PER_CELL         = 3.200   # auto-start the engine
CRITICAL_V_PER_CELL      = 3.100   # start even during quiet hours

# 3.000 V/cell = 42.0 V on 14S, which is what BOTH units already have
# deployed. It is the 0 % point of the curve above and sits well clear of
# the BMS cutoff (~2.5 V/cell = 35 V), leaving room to abort a doomed
# charge attempt before the BMS disconnects for us.
MIN_ABORT_V_PER_CELL     = 3.000

# Internal pack resistance, per cell, in ohms. Used to undo the terminal
# voltage lift while regen current is flowing into the pack:
#     V_resting ≈ V_terminal + motor_amps_in × R_pack
# (VESC signs `motor_amps_in` as current INTO the controller, so current
# into the pack is its negation, which is why this is a plus.)
# Inherited as 0.10 Ω total from an era when the pack was assumed to be
# 15S, hence 1/15th per cell — giving ~0.093 Ω across a 14S pack. Series
# cells add resistance, so per-cell is the physically correct form. The
# magnitude is a rough empirical estimate, never tightly calibrated: raise
# it if the voltage-derived SoC climbs faster than the LCD's coulomb count
# during a charge run.
PACK_RESISTANCE_OHMS_PER_CELL = 0.10 / 15.0

# There is no safe default for a MIXED fleet — this exists only for docs
# seeded before cellCount was a field, and every consumer logs loudly when
# it falls back here.
#
# 14 is chosen deliberately as the least-bad guess, because its failure mode
# is the visible one. Guessing 14 on a 15S pack sets voltageStop too LOW, so
# charging stops early — annoying, obvious, noticed within a cycle. Guessing
# 15 on a 14S pack sets voltageStop above what the pack can physically reach,
# so the charge loop never terminates on voltage and silently degrades into a
# 2-hour timer. That second failure ran undetected on UNIT-002 for weeks.
# Prefer a bug that announces itself.
DEFAULT_CELL_COUNT = 14


def pack_threshold(v_per_cell: float, cell_count: int) -> float:
    """Scale a per-cell threshold to this pack. Rounded to 0.1 V — that is
    the resolution the VESC reports and the precision anyone can act on."""
    return round(v_per_cell * cell_count, 1)


def volts_to_soc(
    volts: Optional[float],
    cell_count: int = DEFAULT_CELL_COUNT,
    amps_in: Optional[float] = None,
) -> Optional[int]:
    """Estimate SoC (0-100) from pack terminal voltage.

    `cell_count` is REQUIRED in spirit even though it defaults — passing the
    wrong one silently produces a plausible, wrong number rather than an
    error, which is precisely the failure this module exists to prevent.

    `amps_in` is the VESC's signed `motor_amps_in`. When supplied, the
    internal-resistance offset is removed first, so the estimate doesn't
    inflate during regen or sag during cranking. Omit it when translating a
    static threshold for display, where no live current applies.

    Returns None if `volts` is missing or not finite.
    """
    if volts is None:
        return None
    try:
        v = float(volts)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):   # NaN / inf
        return None
    if cell_count <= 0:
        return None

    if amps_in is not None:
        try:
            v += float(amps_in) * PACK_RESISTANCE_OHMS_PER_CELL * cell_count
        except (TypeError, ValueError):
            pass

    per_cell = v / cell_count

    if per_cell <= SOC_CURVE_V_PER_CELL[0][0]:
        return 0
    if per_cell >= SOC_CURVE_V_PER_CELL[-1][0]:
        return 100
    for i in range(1, len(SOC_CURVE_V_PER_CELL)):
        hi_v, hi_soc = SOC_CURVE_V_PER_CELL[i]
        if per_cell <= hi_v:
            lo_v, lo_soc = SOC_CURVE_V_PER_CELL[i - 1]
            t = (per_cell - lo_v) / (hi_v - lo_v)
            return round(lo_soc + t * (hi_soc - lo_soc))
    return 100  # unreachable


def describe(cell_count: int) -> str:
    """One-line summary of what this cell count implies — for log lines, so
    a unit's assumed pack geometry is visible in its own logs rather than
    something you have to go look up."""
    return (
        "%dS: full=%.1fV stop=%.1fV start=%.1fV critical=%.1fV abort=%.1fV"
        % (
            cell_count,
            pack_threshold(SOC_CURVE_V_PER_CELL[-1][0], cell_count),
            pack_threshold(STOP_V_PER_CELL, cell_count),
            pack_threshold(START_V_PER_CELL, cell_count),
            pack_threshold(CRITICAL_V_PER_CELL, cell_count),
            pack_threshold(MIN_ABORT_V_PER_CELL, cell_count),
        )
    )


if __name__ == "__main__":
    # Self-check: prove the per-cell curve reproduces the deployed pack
    # thresholds on both units. Run with: python3 pi/voltage_soc.py
    print(describe(15))   # UNIT-001
    print(describe(14))   # UNIT-002
    print()
    # Deployed values on each unit. The fleet is mixed on purpose.
    expected = {
        15: {"stop": 52.5, "start": 48.0, "critical": 46.5},
        14: {"stop": 49.0, "start": 44.8, "critical": 43.4},
    }
    ok = True
    for n, exp in expected.items():
        got = {
            "stop":     pack_threshold(STOP_V_PER_CELL, n),
            "start":    pack_threshold(START_V_PER_CELL, n),
            "critical": pack_threshold(CRITICAL_V_PER_CELL, n),
        }
        for k, want in exp.items():
            flag = "ok" if got[k] == want else "MISMATCH"
            if got[k] != want:
                ok = False
            print("  %dS %-9s want %.1f  got %.1f  %s" % (n, k, want, got[k], flag))
    print()
    print("deployed thresholds reproduced:", ok)
    print()
    for n in (14, 15):
        print("  %dS sample: " % n, end="")
        print("  ".join(
            "%.1fV=%s%%" % (v, volts_to_soc(v, n))
            for v in (pack_threshold(x, n) for x in (3.0, 3.1, 3.2, 3.3, 3.4, 3.5))
        ))
