// VESC input-voltage → SOC estimator for the 14S LiFePO4 pack inside the
// Predator 2000W. Used as a fallback display when the LCD-derived SOC
// from the Predator I²C decoder is null or stale (e.g. LCD sleeping).
//
// LiFePO4 chemistry has a VERY flat middle plateau — ~46.0–46.5 V covers
// roughly 30 %–70 % SOC for a 14S pack. Voltage-based SOC is therefore
// only useful at the extremes (low for "needs charging" triggers, high
// for "charging complete"). We mark this on the dashboard as "(APPROX)"
// so the user knows it isn't a coulomb-counted reading.
//
// Calibration anchor (2026-06-01): VESC reported ~49.9 V while the LCD
// displayed 98 % SOC at a light load. The curve below is set so that data
// point lands at ~97 % — close enough that the two readings agree to
// within ±2 % at the upper end.
//
// Mapping points (pack voltage = cell voltage × 14):
//   ~51.5 V (3.68/cell) → 100 %  (fully charged, may exceed briefly under charge)
//   ~50.5 V (3.61/cell) → 100 %  (resting full)
//   ~49.5 V (3.54/cell) →  95 %
//   ~48.0 V (3.43/cell) →  90 %  ← typical "charge stop" threshold
//   ~47.0 V (3.36/cell) →  80 %
//   ~46.5 V (3.32/cell) →  70 %
//   ~46.0 V (3.29/cell) →  50 %  (middle of the flat plateau)
//   ~45.5 V (3.25/cell) →  30 %
//   ~45.0 V (3.21/cell) →  20 %  ← typical "charge start" threshold
//   ~44.0 V (3.14/cell) →  10 %
//   ~42.0 V (3.00/cell) →   0 %  (BMS cuts soon after)
//
// We linearly interpolate between adjacent points. Values outside the
// range clamp to 0 or 100. If the user ever observes a sharp disagreement
// between this estimate and the LCD's SOC at a known voltage, add a new
// anchor point — the curve will adapt.

interface SocPoint { v: number; soc: number; }

const CURVE: ReadonlyArray<SocPoint> = [
  { v: 42.0, soc: 0 },
  { v: 44.0, soc: 10 },
  { v: 45.0, soc: 20 },
  { v: 45.5, soc: 30 },
  { v: 46.0, soc: 50 },
  { v: 46.5, soc: 70 },
  { v: 47.0, soc: 80 },
  { v: 48.0, soc: 90 },
  { v: 49.5, soc: 95 },
  { v: 50.5, soc: 100 },
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
