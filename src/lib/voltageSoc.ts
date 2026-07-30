// VESC input-voltage → SOC estimator for the LiFePO4 pack inside a Predator
// 2000W. Used as a fallback display when the LCD-derived SOC from the Predator
// I²C decoder is null or stale (e.g. LCD sleeping).
//
// LiFePO4 chemistry has a VERY flat middle plateau — for a 15S pack, ~48.5 to
// 49.0 V spans roughly 30 %–50 % SOC. Voltage-based SOC is therefore only
// really useful at the extremes (low for "needs charging" triggers, high for
// "charging complete"). The dashboard marks it "(APPROX)" so the user knows it
// isn't a coulomb-counted reading.
//
// ── Why this curve is per-cell, not per-pack ───────────────────────────────
// Cell count differs between units: UNIT-001 is 15S (52.8 V resting full),
// UNIT-002 is 14S (49.5 V resting full). Pack voltage is therefore meaningless
// on its own — 49.5 V is a FULL 14S pack but a nearly-flat 15S one. The
// invariant across the fleet is volts *per cell*, which is set by the
// chemistry, so the curve is stored per-cell and scaled by the unit's
// `cellCount` at lookup time.
//
// Copying pack voltages between units is exactly the mistake that produced an
// unreachable 52.5 V charge-stop on the 14S unit. Do not reintroduce a
// pack-voltage constant here.
//
// ── Calibration provenance ────────────────────────────────────────────────
// The anchors below were all measured on the 15S pack, so they are recorded
// as 15S pack volts and divided by 15 once, at module load. That keeps the
// original measurements legible instead of burying them in hand-computed
// per-cell decimals that could drift on the next edit.
//
//   2026-06-05: pack measured 52.8 V resting fully charged → 3.52 V/cell,
//               a healthy LiFePO4 BMS termination point.
//   2026-06-10: LCD displayed 71 % SOC at 50.1 V resting → the 3.340 V/cell
//               anchor, which keeps this estimate within ±2 % of the
//               Predator's coulomb-counted SOC at that point.
//
// Per-cell mapping that results, with the pack voltage each cell figure
// implies at 15S and 14S:
//
//   V/cell   SOC        15S      14S
//   3.5333   100 %     53.0     49.5
//   3.5000    95 %     52.5     49.0   ← engine.charge.voltageStop
//   3.4333    90 %     51.5     48.1
//   3.3667    80 %     50.5     47.1
//   3.3400    71 %     50.1     46.8   ← shop anchor, LCD-confirmed
//   3.3000    60 %     49.5     46.2
//   3.2667    50 %     49.0     45.7   (middle of the flat plateau)
//   3.2333    30 %     48.5     45.3
//   3.2000    20 %     48.0     44.8   ← supervisor.voltageStart
//   3.1000    10 %     46.5     43.4   ← supervisor.voltageCritical
//   3.0667     5 %     46.0     42.9   ← engine.charge.voltageMinAbort
//   3.0000     0 %     45.0     42.0   (BMS cuts out around 2.5 V/cell)
//
// The 14S column reproduces the thresholds configured on UNIT-002 exactly
// (49.0 / 44.8 / 43.4), which is a useful cross-check: the engine thresholds
// and this curve are two independent derivations from the same per-cell
// targets, and they agree.
//
// We linearly interpolate between adjacent points; values outside the range
// clamp to 0 or 100. If the estimate ever disagrees sharply with the LCD at a
// known voltage, add an anchor — the curve adapts.

// Internal-resistance estimate, in ohms PER CELL. Used to undo the
// terminal-voltage boost while regen current is being pumped in:
//   V_resting ≈ V_terminal − I_into_pack × R
// VESC's `motor_amps_in` sign convention is "current into the controller",
// so I_into_pack = −motor_amps_in, and the formula reduces to:
//   V_resting ≈ V_terminal + motor_amps_in × R
//
// Empirical anchor (2026-06-10): at ~10 A regen the dashboard SOC climbed
// quickly during charge, implying roughly 1–1.5 V of terminal lift per 10 A,
// so R ≈ 0.10–0.15 Ω for the whole 15S pack. Starting from 0.10 Ω at 15S.
// Series cells add resistance, so this is stored per-cell and multiplied by
// cellCount — the same reason the voltage curve is per-cell. Tune empirically:
// if displayed SoC still rises faster than the LCD's during a charge run,
// raise this value.
//
// The same compensation applies in reverse during cranking — motor_amps_in
// spikes positive, V_terminal sags, and the formula adds the sag back.
// Inverter discharge through the Predator's own MOSFETs is invisible to the
// VESC and unaffected; the LCD-reported SoC is the source of truth there.
const CELL_INTERNAL_RESISTANCE_OHMS = 0.10 / 15;

interface SocPoint { v: number; soc: number; }

// Cell count of the pack these anchors were measured on. Only used to convert
// the measurements to per-cell; it is NOT a default for unknown units.
const CALIBRATION_CELL_COUNT = 15;

const CURVE_AS_MEASURED_15S: ReadonlyArray<SocPoint> = [
  { v: 45.0, soc: 0 },
  // NB: no anchor at 46.0 V / 5 % (voltageMinAbort). The table above lists it
  // as a reference point, but it has never been an interpolation point and
  // adding one would shift SOC in the low range. Left alone deliberately —
  // this change is only about cell-count scaling.
  { v: 46.5, soc: 10 },   // ← supervisor.voltageCritical
  { v: 48.0, soc: 20 },   // ← supervisor.voltageStart
  { v: 48.5, soc: 30 },
  { v: 49.0, soc: 50 },
  { v: 49.5, soc: 60 },
  { v: 50.1, soc: 71 },   // ← shop anchor, LCD-confirmed 2026-06-10
  { v: 50.5, soc: 80 },
  { v: 51.5, soc: 90 },
  { v: 52.5, soc: 95 },   // ← engine.charge.voltageStop
  { v: 53.0, soc: 100 },
];

// Volts per cell → SOC. The scale-invariant form of the curve above.
const CURVE: ReadonlyArray<SocPoint> = CURVE_AS_MEASURED_15S.map((p) => ({
  v: p.v / CALIBRATION_CELL_COUNT,
  soc: p.soc,
}));

export interface SocOptions {
  /**
   * Series cell count of this unit's pack, from `UnitDoc.cellCount`
   * (UNIT-001 = 15, UNIT-002 = 14).
   *
   * Deliberately has no default. A wrong cell count yields a confidently
   * wrong SOC — a full 14S pack read on the 15S curve looks like ~50 %, which
   * would have the supervisor start the engine on a full battery. Pass
   * null/undefined while the unit doc is still loading and the function
   * returns null, so the UI shows "no estimate" instead of a plausible lie.
   */
  cellCount: number | null | undefined;
  /**
   * The VESC's `motor_amps_in` (signed, amps). When provided, compensates for
   * internal-resistance offset so SOC doesn't inflate during regen or sag
   * during cranking. Omit on settings screens that render threshold
   * approximations, where no live current applies.
   */
  ampsIn?: number | null;
}

/**
 * Estimate state-of-charge as a 0–100 % number from VESC-reported pack
 * voltage, scaled to the unit's cell count.
 *
 * Returns null when `volts` is missing/non-finite, or when `cellCount` is
 * missing or not a positive integer.
 *
 * Options are passed as an object rather than positional arguments on
 * purpose: the previous signature was `(volts, ampsIn)`, and inserting
 * `cellCount` positionally would let an un-updated `(volts, amps)` call site
 * keep compiling while silently reading amps as a cell count. Forcing the
 * object shape makes the compiler flag every caller.
 */
export function vescVoltsToSoc(
  volts: number | null | undefined,
  options: SocOptions,
): number | null {
  if (volts === null || volts === undefined || !Number.isFinite(volts)) return null;

  const { cellCount, ampsIn } = options;
  if (
    cellCount === null ||
    cellCount === undefined ||
    !Number.isFinite(cellCount) ||
    cellCount <= 0
  ) {
    return null;
  }

  const packResistance = CELL_INTERNAL_RESISTANCE_OHMS * cellCount;
  const restingVolts =
    typeof ampsIn === 'number' && Number.isFinite(ampsIn)
      ? volts + ampsIn * packResistance
      : volts;

  const perCell = restingVolts / cellCount;

  if (perCell <= CURVE[0].v) return 0;
  if (perCell >= CURVE[CURVE.length - 1].v) return 100;
  for (let i = 1; i < CURVE.length; i++) {
    const hi = CURVE[i];
    if (perCell <= hi.v) {
      const lo = CURVE[i - 1];
      const t = (perCell - lo.v) / (hi.v - lo.v);
      return Math.round(lo.soc + t * (hi.soc - lo.soc));
    }
  }
  return 100; // unreachable; satisfies TS
}

/**
 * Pack voltage that corresponds to a given per-cell voltage, for a unit with
 * `cellCount` cells. Handy for rendering threshold explanations ("stops at
 * 49.0 V = 3.50 V/cell") without hardcoding pack numbers per unit.
 */
export function cellVoltsToPackVolts(
  perCellVolts: number,
  cellCount: number | null | undefined,
): number | null {
  if (
    cellCount === null ||
    cellCount === undefined ||
    !Number.isFinite(cellCount) ||
    cellCount <= 0
  ) {
    return null;
  }
  return perCellVolts * cellCount;
}
