// VESC input-voltage → SOC estimator for the LiFePO4 pack inside the
// Predator 2000W. Used as a fallback display when the LCD-derived SOC from
// the Predator I²C decoder is null or stale (e.g. LCD sleeping/dark).
//
// THE FLEET IS MIXED: UNIT-001 is 15S, UNIT-002 is 14S. The curve below is
// expressed PER CELL and MUST be multiplied by the unit's
// config/engine.cellCount at the point of use — never assume a pack size.
// Keep it identical to SOC_CURVE_V_PER_CELL in pi/voltage_soc.py, which
// carries the full rationale, the calibration provenance, and the method
// for settling cell count from telemetry.
//
// LiFePO4 has a VERY flat middle plateau — ~3.23–3.27 V/cell spans roughly
// 30 %–50 % SOC. Voltage-based SOC is therefore only useful near the
// extremes (low for "needs charging", high for "charging complete"). The
// dashboard marks it "(APPROX)" so it reads as an estimate, not a
// coulomb-counted measurement.
//
// Anchors, both measured on UNIT-001 (15S): 52.8 V resting full =
// 3.520 V/cell (2026-06-05), and 50.1 V @ LCD 71 % = 3.340 V/cell
// (2026-06-10). Cross-checked on the 14S pack: the curve's 100 % point
// (3.5333 V/cell) is 49.5 V, matching UNIT-002's measured resting-full
// (multimeter, 2026-07-29). The same per-cell shape is anchored
// independently on both packs.
//
// Internal pack resistance, per cell, in ohms. Undoes the terminal-voltage
// lift while regen current is being pumped in:
//   V_resting ≈ V_terminal − I_into_pack × R
// VESC signs `motor_amps_in` as current INTO the controller, so
// I_into_pack = −motor_amps_in and the formula reduces to:
//   V_resting ≈ V_terminal + motor_amps_in × R
// Inherited as 0.10 Ω across the whole pack from the 15S era, hence 1/15th
// per cell (~0.093 Ω on 14S). A rough empirical figure, never tightly
// calibrated — raise it if displayed SoC climbs faster than the LCD's
// coulomb count during a charge run. The same compensation applies in
// reverse while cranking, when motor_amps_in spikes positive and the
// terminal voltage sags. Inverter discharge through the Predator's own
// MOSFETs is invisible to the VESC and unaffected; the LCD SoC is the
// source of truth in that case.
const PACK_RESISTANCE_OHMS_PER_CELL = 0.10 / 15;

// ─── Per-cell curve — MIRROR OF pi/voltage_soc.py ─────────────────────────
// Both this file and the Python supervisor used to carry their own copy of
// the curve in PACK volts, each with a comment asking the next editor to
// hand-update the other. They drifted regardless — the two disagreed by 10
// points at 49.5 V — and the pack-volt form produced badly wrong numbers on
// the real 14S packs, where a full battery reads as ~50 % on a 15S curve.
//
// Per-cell fixes both: the curve is a property of LiFePO4 chemistry, not of
// one particular pack, and multiplying by cellCount at the point of use
// makes a wrong cell count a visible input rather than a silent assumption.
// Keep these values identical to SOC_CURVE_V_PER_CELL in pi/voltage_soc.py.
interface SocPoint { vPerCell: number; soc: number; }

const CURVE_PER_CELL: ReadonlyArray<SocPoint> = [
  { vPerCell: 3.0000, soc: 0 },
  { vPerCell: 3.1000, soc: 10 },   // ← supervisor.voltageCritical
  { vPerCell: 3.2000, soc: 20 },   // ← supervisor.voltageStart
  { vPerCell: 3.2333, soc: 30 },
  { vPerCell: 3.2667, soc: 50 },
  { vPerCell: 3.3000, soc: 60 },
  { vPerCell: 3.3400, soc: 71 },   // ← anchor: 50.1 V @ LCD 71 % on 15S, 2026-06-10
  { vPerCell: 3.3667, soc: 80 },
  { vPerCell: 3.4333, soc: 90 },
  { vPerCell: 3.5000, soc: 95 },   // ← engine.charge.voltageStop (49.0 V on 14S)
  { vPerCell: 3.5333, soc: 100 }, // resting full: 52.8 V on 15S / 49.5 V on 14S
];

// Used when config/engine.cellCount is absent (docs seeded before the field
// existed). There is no safe default for a mixed fleet; treat a missing
// cellCount as a provisioning bug. 14 is the least-bad guess because it
// fails visibly (charging stops early) rather than silently (voltageStop
// unreachable → the charge loop degrades into a 2-hour timer).
export const DEFAULT_CELL_COUNT = 14;

/** Scale a per-cell threshold to a pack voltage, e.g. 3.5 × 14 → 49.0. */
export function packThreshold(vPerCell: number, cellCount: number): number {
  return Math.round(vPerCell * cellCount * 10) / 10;
}

/**
 * Estimate state-of-charge as a 0-100 % number from VESC-reported pack
 * voltage. Returns null when `volts` is missing or non-finite.
 *
 * Optional `ampsIn` is the VESC's `motor_amps_in` value (signed, in amps).
 * When provided, the function compensates for the internal-resistance
 * voltage offset before looking up the curve, so the displayed SoC
 * doesn't inflate during regen or sag during cranking. Omit (or pass
 * null/undefined) to look up the raw terminal voltage — appropriate for
 * displaying threshold approximations on a settings screen where no
 * live current measurement applies.
 */
export function vescVoltsToSoc(
  volts: number | null | undefined,
  cellCount: number = DEFAULT_CELL_COUNT,
  ampsIn?: number | null,
): number | null {
  if (volts === null || volts === undefined || !Number.isFinite(volts)) return null;
  if (!Number.isFinite(cellCount) || cellCount <= 0) return null;

  const resistance = PACK_RESISTANCE_OHMS_PER_CELL * cellCount;
  const restingVolts =
    typeof ampsIn === 'number' && Number.isFinite(ampsIn)
      ? volts + ampsIn * resistance
      : volts;

  const perCell = restingVolts / cellCount;
  if (perCell <= CURVE_PER_CELL[0].vPerCell) return 0;
  if (perCell >= CURVE_PER_CELL[CURVE_PER_CELL.length - 1].vPerCell) return 100;
  for (let i = 1; i < CURVE_PER_CELL.length; i++) {
    const hi = CURVE_PER_CELL[i];
    if (perCell <= hi.vPerCell) {
      const lo = CURVE_PER_CELL[i - 1];
      const t = (perCell - lo.vPerCell) / (hi.vPerCell - lo.vPerCell);
      return Math.round(lo.soc + t * (hi.soc - lo.soc));
    }
  }
  return 100; // unreachable; satisfies TS
}
