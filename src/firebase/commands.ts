// Typed command issuers.
//
// Screens issue intents by calling these helpers — never by writing to
// `units/{unitId}/commands` directly. This keeps payload shapes consistent
// with the Pi-side dispatcher and makes every call site grep-able.
//
// Each helper returns the new command's doc id so callers can subscribe
// to its status if they want optimistic-update + rollback semantics later.

import {
  addDoc,
  collection,
  serverTimestamp,
} from 'firebase/firestore';
import { getFirebase } from './config';
import type {
  CommandKind,
  ChargeConfig,
  LightConfig,
  RelaysConfig,
  SentryConfig,
} from './types';

async function issueCommand(
  unitId: string,
  issuedBy: string,
  kind: CommandKind,
  payload: Record<string, unknown>,
): Promise<string> {
  const { db } = getFirebase();
  // issuedAt is a server-timestamp sentinel on write, materializes to a
  // Timestamp on the Pi side — keep the write shape loose so TS doesn't
  // try to reconcile FieldValue with the strict CommandDoc.issuedAt type.
  const ref = await addDoc(collection(db, 'units', unitId, 'commands'), {
    kind,
    issuedBy,
    issuedAt: serverTimestamp(),
    payload,
    status: 'pending',
  });
  return ref.id;
}

// ── light & relays (phase B) ────────────────────────────────────────────
export function setLight(unitId: string, uid: string, mode: 'off' | 'on' | 'auto') {
  return issueCommand(unitId, uid, 'light.set', { mode });
}

export function setRelay(
  unitId: string,
  uid: string,
  channel: 1 | 2 | 3,
  mode: 'off' | 'on' | 'auto',
) {
  return issueCommand(unitId, uid, 'relay.set', { channel, mode });
}

export function updateLightConfig(unitId: string, uid: string, patch: Partial<LightConfig>) {
  return issueCommand(unitId, uid, 'light.set', { configPatch: patch });
}

export function updateRelaysConfig(unitId: string, uid: string, config: RelaysConfig) {
  return issueCommand(unitId, uid, 'relay.set', { configReplace: config });
}

// ── engine recharge schedule (phase C) ──────────────────────────────────
export function overrideEngine(
  unitId: string,
  uid: string,
  action: 'run_now' | 'stop' | 'allow_quiet_once',
) {
  return issueCommand(unitId, uid, 'engine.override', { action });
}

export function updateChargeConfig(unitId: string, uid: string, patch: Partial<ChargeConfig>) {
  return issueCommand(unitId, uid, 'charge.update', { configPatch: patch });
}

// ── sentry + camera (phase D) ───────────────────────────────────────────
export function armSentry(unitId: string, uid: string) {
  return issueCommand(unitId, uid, 'sentry.arm', {});
}

export function disarmSentry(unitId: string, uid: string) {
  return issueCommand(unitId, uid, 'sentry.disarm', {});
}

export function updateSentryConfig(unitId: string, uid: string, patch: Partial<SentryConfig>) {
  return issueCommand(unitId, uid, 'sentry.update', { configPatch: patch });
}

export function startCameraStream(unitId: string, uid: string) {
  return issueCommand(unitId, uid, 'camera.startStream', {});
}

export function stopCameraStream(unitId: string, uid: string) {
  return issueCommand(unitId, uid, 'camera.stopStream', {});
}

// ── LCD wake button (phase E.2) ─────────────────────────────────────────
// Triggers a single press-and-release of the Predator's LCD wake button
// via the PCA9685 ch2 servo. `pressDurationSec` overrides the default
// (config/lcdWake.pressDurationSec) for this one press only — leave it
// undefined to use the configured default.
export function wakeLcd(unitId: string, uid: string, pressDurationSec?: number) {
  const payload: Record<string, unknown> = {};
  if (pressDurationSec !== undefined) payload.pressDurationSec = pressDurationSec;
  return issueCommand(unitId, uid, 'lcd.wake', payload);
}

// ── AC outlet toggle (phase E.3) ────────────────────────────────────────
// Triggers a single press-and-release of the Predator's AC power button
// via the PCA9685 ch3 servo. Each press *flips* the AC outlet state
// (on→off or off→on); the Pi has no way to know which direction it went.
// Callers should be ready for either outcome.
export function toggleAc(unitId: string, uid: string, pressDurationSec?: number) {
  const payload: Record<string, unknown> = {};
  if (pressDurationSec !== undefined) payload.pressDurationSec = pressDurationSec;
  return issueCommand(unitId, uid, 'ac.toggle', payload);
}

// ── engine starter cranking (phase G.2) ─────────────────────────────────
// Single crank attempt via the VESC motor controller. Pi-side `engine.py`
// runs a safety loop that refreshes SET_CURRENT at 10 Hz, watches motor
// current for the engine-catch signal, and aborts on catch OR timeout.
// `currentAmps` and `maxDurationSec` override the config-level defaults
// for this one attempt; both are clamped to hard safety ceilings on the Pi.
export function crankEngine(
  unitId: string,
  uid: string,
  override?: { currentAmps?: number; maxDurationSec?: number },
) {
  const payload: Record<string, unknown> = {};
  if (override?.currentAmps !== undefined) payload.currentAmpsOverride = override.currentAmps;
  if (override?.maxDurationSec !== undefined) payload.maxDurationSecOverride = override.maxDurationSec;
  return issueCommand(unitId, uid, 'engine.crank', payload);
}

// ── engine full-start choreography (phase G.3) ──────────────────────────
// Runs the full sequence: set choke → spark relay on → crank → on-catch
// stabilize + open choke. On failure (timeout / no load), the choke is
// returned to idle and the spark relay is turned back off automatically.
// This is the user-facing "Start Engine" action; engine.crank stays
// available as a low-level primitive for bench testing.
export function startEngine(
  unitId: string,
  uid: string,
  override?: {
    chokePreset?: 'start_cold' | 'start_warm' | string;
    currentAmps?: number;
    maxDurationSec?: number;
  },
) {
  const payload: Record<string, unknown> = {};
  if (override?.chokePreset !== undefined) payload.chokePreset = override.chokePreset;
  if (override?.currentAmps !== undefined) payload.currentAmpsOverride = override.currentAmps;
  if (override?.maxDurationSec !== undefined) payload.maxDurationSecOverride = override.maxDurationSec;
  return issueCommand(unitId, uid, 'engine.start', payload);
}

// ── regen charging (phase G.4) ──────────────────────────────────────────
// Spawn the background charge loop. Pi-side `engine.py` runs an async
// SET_CURRENT(-N) loop with voltage / RPM / temperature safety checks.
// Returns once the loop is spawned (the actual charging runs for minutes
// to hours). Use stopEngine() to interrupt.
export function chargeEngine(
  unitId: string,
  uid: string,
  override?: { currentAmps?: number; voltageStop?: number; maxDurationSec?: number },
) {
  const payload: Record<string, unknown> = {};
  if (override?.currentAmps !== undefined) payload.currentAmpsOverride = override.currentAmps;
  if (override?.voltageStop !== undefined) payload.voltageStopOverride = override.voltageStop;
  if (override?.maxDurationSec !== undefined) payload.maxDurationSecOverride = override.maxDurationSec;
  return issueCommand(unitId, uid, 'engine.charge', payload);
}

// Graceful shutdown: aborts any active charge loop, opens the spark relay,
// waits for RPM to fall under idle threshold.
export function stopEngine(unitId: string, uid: string) {
  return issueCommand(unitId, uid, 'engine.stop', {});
}
