// Sensorless fuel model.
//
// The unit has no fuel-level sensor. We estimate everything from two inputs
// the system already produces:
//   1. engine.start / engine.stop events → generator runtime (paired here).
//   2. user-logged fuel.refuel events    → level anchors + calibration.
//
// Burn rate `r` (gal/hr) is the one unknown. It's seeded with a prior and
// self-calibrated: between two refuels the fuel poured equals the fuel burned,
// so r = poured / runtime. A "ran dry" engine stop (reason 'stalled' while the
// battery wasn't full) gives an even cleaner anchor: level ≈ 0, so
// r = levelBeforeLastFill / runtimeSinceLastFill.
//
// Everything in this file is pure and deterministic — no React, no Firestore —
// so it's trivially testable. The hook (useFuelModel) feeds it raw events.

import type { FuelSettings } from '../firebase/types';

// A minimal, source-agnostic view of an event the model cares about.
export interface RawFuelEvent {
  kind: string;
  atMs: number;
  payload: Record<string, unknown>;
}

export interface EngineSession {
  startMs: number;
  endMs: number;
}

export type FuelAlert = 'ok' | 'low' | 'empty';

export interface FuelModel {
  // Level / tank
  level: number | null;               // gallons; null until the first refuel
  tankGallons: number;
  pctTank: number | null;             // 0–1

  // Burn rate in effect
  burnRateGalPerHour: number;
  calibrated: boolean;                // ≥1 accepted calibration sample
  calibrationSamples: number;

  // Since-last-refuel
  lastRefuelMs: number | null;
  runtimeSinceRefuelHours: number | null;
  burnedSinceRefuelGallons: number | null;
  costSinceRefuel: number | null;

  // Trend / projection
  dailyBurnGallons: number | null;    // trailing-window average, gal/day
  runwayDays: number | null;          // null when burn ≈ 0
  projectedEmptyMs: number | null;
  runtimeTodayHours: number;
  sparkline: number[];                // gal burned per 24h bucket, oldest→newest

  // Status
  engineRunning: boolean;
  ranDryDetected: boolean;            // dry stop since the last refuel
  alert: FuelAlert;
}

export const FUEL_DEFAULTS = {
  tankGallons: 5.0,
  seedBurnRateGalPerHour: 0.2,
} as const;

const R_MIN = 0.02;                   // gal/hr — reject calibration below this
const R_MAX = 1.0;                    // gal/hr — reject calibration above this
const EMA_ALPHA = 0.5;                // calibration smoothing weight
const MIN_CALIB_RUNTIME_HOURS = 0.5;  // ignore intervals with trivial runtime
const WINDOW_DAYS = 14;               // trailing window for daily-burn average
const SPARK_DAYS = 14;                // sparkline buckets
const LOW_PCT = 0.15;                 // low-fuel warning: tank fraction
const LOW_RUNWAY_DAYS = 1;            // low-fuel warning: projected runway
const DRY_SOC_FULL = 95;             // socAtStop ≥ this ⇒ not a dry stop
const DRY_PLAUSIBLE_LOW = 0.3;        // dry-stop runtime sanity band (× expected)
const DRY_PLAUSIBLE_HIGH = 3.0;
const DAY_MS = 86_400_000;

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

interface RefuelRecord {
  atMs: number;
  method: 'full' | 'add';
  gallons: number | null;             // amount poured, if known
  levelAfter: number;                 // tank level right after the fill
}

interface DryStop {
  atMs: number;
}

// Overlap of [a,b] with each session, summed, in hours.
function makeRuntimeFn(sessions: EngineSession[]) {
  return (aMs: number, bMs: number): number => {
    if (bMs <= aMs) return 0;
    let ms = 0;
    for (const s of sessions) {
      const lo = Math.max(s.startMs, aMs);
      const hi = Math.min(s.endMs, bMs);
      if (hi > lo) ms += hi - lo;
    }
    return ms / 3_600_000;
  };
}

export function computeFuelModel(
  rawEvents: RawFuelEvent[],
  settings: FuelSettings,
  nowMs: number,
): FuelModel {
  const events = [...rawEvents].sort((a, b) => a.atMs - b.atMs);
  const tank = settings.tankGallons;

  // ── 1. Pair engine start/stop into runtime sessions ──────────────────────
  const sessions: EngineSession[] = [];
  let openStart: number | null = null;
  for (const e of events) {
    if (e.kind === 'engine.start') {
      // A start while one is already open means we dropped the stop — close
      // the prior session at this start (conservative: counts the gap).
      if (openStart !== null) sessions.push({ startMs: openStart, endMs: e.atMs });
      openStart = e.atMs;
    } else if (e.kind === 'engine.stop') {
      if (openStart !== null) {
        sessions.push({ startMs: openStart, endMs: e.atMs });
        openStart = null;
      }
      // A stop with no open start means we dropped the start — ignore it.
    }
  }
  const engineRunning = openStart !== null;
  if (openStart !== null) sessions.push({ startMs: openStart, endMs: nowMs });

  const runtime = makeRuntimeFn(sessions);

  // ── 2. Collect refuels and dry stops ─────────────────────────────────────
  const refuels: RefuelRecord[] = [];
  const dryStops: DryStop[] = [];
  for (const e of events) {
    if (e.kind === 'fuel.refuel') {
      const method = e.payload.method === 'add' ? 'add' : 'full';
      const gallons =
        typeof e.payload.gallons === 'number' ? e.payload.gallons : null;
      const levelAfter =
        typeof e.payload.levelAfter === 'number'
          ? e.payload.levelAfter
          : method === 'full'
            ? tank
            : (gallons ?? 0);
      refuels.push({ atMs: e.atMs, method, gallons, levelAfter });
    } else if (e.kind === 'engine.stop') {
      // Dry-stop signal (needs the Pi payload enrichment; absent on older
      // firmware, in which case this simply never fires).
      if (e.payload.reason === 'stalled') {
        const soc = e.payload.socAtStop;
        const batteryFull = typeof soc === 'number' && soc >= DRY_SOC_FULL;
        if (!batteryFull) dryStops.push({ atMs: e.atMs });
      }
    }
  }

  // ── 3. Calibrate burn rate ───────────────────────────────────────────────
  // Fold all calibration anchors in chronological order via EMA, starting
  // from the seed prior. Two anchor kinds:
  //   • refuel interval  — consumed = levelAfter_prev − (levelAfter_cur − pouredAtCur)
  //   • dry stop         — consumed = levelAfter_prev (tank ran to ~0)
  interface Anchor { atMs: number; consumed: number; runtimeH: number; dry: boolean }
  const anchors: Anchor[] = [];

  for (let i = 1; i < refuels.length; i++) {
    const prev = refuels[i - 1];
    const cur = refuels[i];
    if (cur.gallons == null) continue; // closing fill amount unknown → can't close the equation
    const consumed = prev.levelAfter - (cur.levelAfter - cur.gallons);
    anchors.push({ atMs: cur.atMs, consumed, runtimeH: runtime(prev.atMs, cur.atMs), dry: false });
  }
  for (const d of dryStops) {
    // Most recent refuel before the dry stop.
    let prev: RefuelRecord | null = null;
    for (const r of refuels) {
      if (r.atMs <= d.atMs) prev = r;
      else break;
    }
    if (!prev) continue;
    anchors.push({ atMs: d.atMs, consumed: prev.levelAfter, runtimeH: runtime(prev.atMs, d.atMs), dry: true });
  }
  anchors.sort((a, b) => a.atMs - b.atMs);

  let r = clamp(settings.seedBurnRateGalPerHour, R_MIN, R_MAX);
  let samples = 0;
  for (const a of anchors) {
    if (a.runtimeH < MIN_CALIB_RUNTIME_HOURS || a.consumed <= 0) continue;
    const rObs = a.consumed / a.runtimeH;
    if (rObs < R_MIN || rObs > R_MAX) continue;
    if (a.dry) {
      // Reject implausible dry stops (a fault that killed the engine early
      // looks like a dry stop but burned far less than a tank).
      const expected = a.consumed / r;
      if (a.runtimeH < DRY_PLAUSIBLE_LOW * expected || a.runtimeH > DRY_PLAUSIBLE_HIGH * expected) {
        continue;
      }
    }
    r = clamp(EMA_ALPHA * rObs + (1 - EMA_ALPHA) * r, R_MIN, R_MAX);
    samples += 1;
  }

  // ── 4. Current level ─────────────────────────────────────────────────────
  const lastRefuel = refuels.length ? refuels[refuels.length - 1] : null;
  let level: number | null = null;
  let pctTank: number | null = null;
  let runtimeSinceRefuelHours: number | null = null;
  let burnedSinceRefuelGallons: number | null = null;
  let costSinceRefuel: number | null = null;
  if (lastRefuel) {
    runtimeSinceRefuelHours = runtime(lastRefuel.atMs, nowMs);
    burnedSinceRefuelGallons = r * runtimeSinceRefuelHours;
    level = clamp(lastRefuel.levelAfter - burnedSinceRefuelGallons, 0, tank);
    pctTank = tank > 0 ? level / tank : null;
    if (typeof settings.pricePerGallon === 'number') {
      costSinceRefuel = burnedSinceRefuelGallons * settings.pricePerGallon;
    }
  }

  const ranDryDetected =
    lastRefuel != null && dryStops.some((d) => d.atMs > lastRefuel.atMs);

  // ── 5. Trend / projection ────────────────────────────────────────────────
  const firstEventMs = events.length ? events[0].atMs : nowMs;
  const windowStart = Math.max(nowMs - WINDOW_DAYS * DAY_MS, firstEventMs);
  const windowDays = Math.max(1, (nowMs - windowStart) / DAY_MS);
  const dailyBurnGallons = (r * runtime(windowStart, nowMs)) / windowDays;

  let runwayDays: number | null = null;
  let projectedEmptyMs: number | null = null;
  if (level != null && dailyBurnGallons > 1e-6) {
    runwayDays = level / dailyBurnGallons;
    projectedEmptyMs = nowMs + runwayDays * DAY_MS;
  }

  const startOfToday = (() => {
    const d = new Date(nowMs);
    d.setHours(0, 0, 0, 0);
    return d.getTime();
  })();
  const runtimeTodayHours = runtime(startOfToday, nowMs);

  const sparkline: number[] = [];
  for (let i = SPARK_DAYS - 1; i >= 0; i--) {
    const bStart = nowMs - (i + 1) * DAY_MS;
    const bEnd = nowMs - i * DAY_MS;
    sparkline.push(r * runtime(bStart, bEnd));
  }

  // ── 6. Alert state ───────────────────────────────────────────────────────
  let alert: FuelAlert = 'ok';
  if (level != null) {
    if (level <= 0.001 || ranDryDetected) {
      alert = 'empty';
    } else if (
      (pctTank != null && pctTank < LOW_PCT) ||
      (runwayDays != null && runwayDays < LOW_RUNWAY_DAYS)
    ) {
      alert = 'low';
    }
  }

  return {
    level,
    tankGallons: tank,
    pctTank,
    burnRateGalPerHour: r,
    calibrated: samples > 0,
    calibrationSamples: samples,
    lastRefuelMs: lastRefuel?.atMs ?? null,
    runtimeSinceRefuelHours,
    burnedSinceRefuelGallons,
    costSinceRefuel,
    dailyBurnGallons,
    runwayDays,
    projectedEmptyMs,
    runtimeTodayHours,
    sparkline,
    engineRunning,
    ranDryDetected,
    alert,
  };
}
