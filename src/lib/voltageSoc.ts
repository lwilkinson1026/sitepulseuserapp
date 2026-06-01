// VESC input-voltage → SOC estimator for the 14S LiFePO4 pack inside the
// Predator 2000W. Used as a fallback display when the LCD-derived SOC
// from the Predator I²C decoder is null or stale (e.g. LCD sleeping).
//
// LiFePO4 voltage-vs-SOC is flat in the middle of the curve (~3.2 V/cell
// covers ~30-80 % SOC), so this is approximate — best used for
// "low / medium / high" hysteresis decisions, not precise gas-gauge UI.
// Anywhere we show this on the dashboard we mark it as "approx" so the
// user understands it's not a true coulomb-counted reading.
//
// Mapping points (pack voltage = cell voltage × 14):
//   ~58.0 V (4.14/cell) → 100 % (high, charger-active territory)
//   ~54.5 V (3.89/cell) → 95 %
//   ~54.0 V (3.86/cell) → 90 %  ← typical "charge stop" threshold
//   ~52.5 V (3.75/cell) → 70 %
//   ~51.0 V (3.64/cell) → 50 %
//   ~49.0 V (3.50/cell) → 30 %
//   ~47.5 V (3.39/cell) → 25 %
//   ~46.5 V (3.32/cell) → 20 %  ← typical "charge start" threshold
//   ~46.0 V (3.29/cell) → 10 %
//   ~44.0 V (3.14/cell) →  0 %  (deep discharge — BMS will cut soon)
//
// We linearly interpolate between adjacent points. Values outside the
// range clamp to 0 or 100.

interface SocPoint { v: number; soc: number; }

const CURVE: ReadonlyArray<SocPoint> = [
  { v: 44.0, soc: 0 },
  { v: 46.0, soc: 10 },
  { v: 46.5, soc: 20 },
  { v: 47.5, soc: 25 },
  { v: 49.0, soc: 30 },
  { v: 51.0, soc: 50 },
  { v: 52.5, soc: 70 },
  { v: 54.0, soc: 90 },
  { v: 54.5, soc: 95 },
  { v: 58.0, soc: 100 },
];

/**
 * Estimate state-of-charge as a 0-100 % number from VESC-reported pack
 * voltage. Returns null when `volts` is missing or non-finite.
 */
export function vescVoltsToSoc(volts: number | null | undefined): number | null {
  if (volts === null || volts === undefined || !Number.isFinite(volts)) return null;
  if (volts <= CURVE[0].v) return 0;
  if (volts >= CURVE[CURVE.length - 1].v) return 100;
  for (let i = 1; i < CURVE.length; i++) {
    const hi = CURVE[i];
    if (volts <= hi.v) {
      const lo = CURVE[i - 1];
      const t = (volts - lo.v) / (hi.v - lo.v);
      return Math.round(lo.soc + t * (hi.soc - lo.soc));
    }
  }
  return 100; // unreachable; satisfies TS
}
