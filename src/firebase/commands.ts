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
