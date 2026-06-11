// VESC input-voltage → SOC estimator for the 15S LiFePO4 pack inside the
// Predator 2000W. Used as a fallback display when the LCD-derived SOC
// from the Predator I²C decoder is null or stale (e.g. LCD sleeping).
//
// LiFePO4 chemistry has a VERY flat middle plateau — ~48.5–49.0 V covers
// roughly 30 %–50 % SOC for a 15S pack. Voltage-based SOC is therefore
// only useful at the extremes (low for "needs charging" triggers, high
// for "charging complete"). We mark this on the dashboard as "(APPROX)"
// so the user knows it isn't a coulomb-counted reading.
//
// Cell-count calibration (2026-06-05): pack measured 52.8 V resting fully
// charged. That's 3.52 V/cell for 15S — healthy LiFePO4 BMS termination
// point. The codebase previously assumed 14S, which made 52.8 V impossible
// (would be 3.77 V/cell, well past LiFePO4 max). 15S × 3.2 V nominal also
// matches the "48 V nominal" label on the pack.
//
// Empirical anchor (2026-06-10): LCD displayed 71 % SOC at 50.1 V resting.
// Curve below threads that point so the voltage fallback agrees with the
// Predator's coulomb-counted SOC within ±2 % at the calibration point.
//
// Mapping points (pack voltage = cell voltage × 15):
//   ~53.0 V (3.53/cell) → 100 %  (resting full per shop measurement)
//   ~52.5 V (3.50/cell) →  95 %  ← engine.charge.voltageStop
//   ~51.5 V (3.43/cell) →  90 %
//   ~50.5 V (3.37/cell) →  80 %
//   ~50.1 V (3.34/cell) →  71 %  ← shop anchor, LCD-confirmed 2026-06-10
//   ~49.5 V (3.30/cell) →  60 %
//   ~49.0 V (3.27/cell) →  50 %  (middle of the flat plateau)
//   ~48.5 V (3.23/cell) →  30 %
//   ~48.0 V (3.20/cell) →  20 %  ← supervisor.voltageStart
//   ~46.5 V (3.10/cell) →  10 %  ← supervisor.voltageCritical
//   ~46.0 V (3.07/cell) →   ~5 % ← engine.charge.voltageMinAbort
//   ~45.0 V (3.00/cell) →   0 %  (BMS cuts soon after at ~2.5 V/cell ≈ 37.5 V)
//
// We linearly interpolate between adjacent points. Values outside the
// range clamp to 0 or 100. If the user ever observes a sharp disagreement
// between this estimate and the LCD's SOC at a known voltage, add a new
// anchor point — the curve will adapt.

// Internal-resistance estimate for the 15S pack, in ohms. Used to undo
// the terminal-voltage boost while regen current is being pumped in:
//   V_resting ≈ V_terminal − I_into_pack × R
// VESC's `motor_amps_in` sign convention is "current into the controller",
// so I_into_pack = −motor_amps_in, and the formula reduces to:
//   V_resting ≈ V_terminal + motor_amps_in × R
// Empirical anchor (2026-06-10): at ~10 A regen the dashboard SOC jumped
// quickly during charge, suggesting roughly 1–1.5 V of terminal lift per
// 10 A — implying R ≈ 0.10–0.15 Ω total pack. Starting at 0.10 Ω, which
// matches an aged 15S 18650/prismatic pack of this size. Tune empirically:
// if the displayed SoC still rises noticeably faster than the LCD's
// coulomb-counted SoC during a charge run, raise this value.
//
// Same compensation applies in reverse during cranking — motor_amps_in
// spikes positive, V_terminal sags, formula adds the sag back. Inverter
// discharge through the Predator's own MOSFETs is invisible to the VESC
// and unaffected; the LCD-reported SoC is the source of truth in that case.
const PACK_INTERNAL_RESISTANCE_OHMS = 0.10;

interface SocPoint { v: number; soc: number; }

const CURVE: ReadonlyArray<SocPoint> = [
  { v: 45.0, soc: 0 },
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
  ampsIn?: number | null,
): number | null {
  if (volts === null || volts === undefined || !Number.isFinite(volts)) return null;
  const restingVolts =
    typeof ampsIn === 'number' && Number.isFinite(ampsIn)
      ? volts + ampsIn * PACK_INTERNAL_RESISTANCE_OHMS
      : volts;
  if (restingVolts <= CURVE[0].v) return 0;
  if (restingVolts >= CURVE[CURVE.length - 1].v) return 100;
  for (let i = 1; i < CURVE.length; i++) {
    const hi = CURVE[i];
    if (restingVolts <= hi.v) {
      const lo = CURVE[i - 1];
      const t = (restingVolts - lo.v) / (hi.v - lo.v);
      return Math.round(lo.soc + t * (hi.soc - lo.soc));
    }
  }
  return 100; // unreachable; satisfies TS
}
