// One-shot setup: find the signed-in user, create units/UNIT-001 with them
// as the owner so the app's security rules let them read live telemetry.
//
// Usage:
//   node scripts/setup-unit.mjs                          # picks the only auth user
//   OWNER_EMAIL=landonw1@mac.com node scripts/setup-unit.mjs
//   UNIT_ID=UNIT-007 OWNER_EMAIL=foo@bar node scripts/setup-unit.mjs
//
// Optional:
//   MODEL=…        model label (default SITEPULSE V1)
//   ENGINE_TYPE=…  recorded on the unit doc when the unit's engine differs
//                  from the fleet's
//
// Refuses to reassign a unit that already has a different owner — transfer
// ownership deliberately, not as a side effect of setup.
//
// Firestore/Auth: uses the service-account file if present, otherwise your
// own Google login via Application Default Credentials — run
// `gcloud auth application-default login` once first.

import { existsSync, readFileSync } from 'node:fs';
import { initializeApp, cert } from 'firebase-admin/app';
import { getAuth } from 'firebase-admin/auth';
import { getFirestore, FieldValue } from 'firebase-admin/firestore';

const UNIT_ID = process.env.UNIT_ID ?? 'UNIT-001';
const SA_PATH = process.env.SERVICE_ACCOUNT ?? './scripts/service-account.json';
const TARGET_EMAIL = process.env.OWNER_EMAIL ?? null;
const MODEL = process.env.MODEL ?? 'SITEPULSE V1';
const ENGINE_TYPE = process.env.ENGINE_TYPE ?? null;

if (existsSync(SA_PATH)) {
  initializeApp({ credential: cert(JSON.parse(readFileSync(SA_PATH, 'utf-8'))) });
} else {
  initializeApp({ projectId: process.env.FIREBASE_PROJECT ?? 'sitepulse-userapp' });
}
const auth = getAuth();
const db = getFirestore();

async function pickUser() {
  if (TARGET_EMAIL) {
    return auth.getUserByEmail(TARGET_EMAIL);
  }
  const { users } = await auth.listUsers(10);
  if (users.length === 0) {
    throw new Error('No auth users found. Sign up in the app first.');
  }
  if (users.length > 1) {
    console.log('Multiple users found:');
    for (const u of users) console.log(`  ${u.email}  ${u.uid}`);
    throw new Error('Re-run with OWNER_EMAIL=… to pick one.');
  }
  return users[0];
}

const user = await pickUser();
console.log(`Owner: ${user.email}  (uid: ${user.uid})`);

const existing = await db.doc(`units/${UNIT_ID}`).get();
const currentOwner = existing.exists ? existing.get('ownerId') : null;
if (currentOwner && currentOwner !== user.uid) {
  throw new Error(`${UNIT_ID} is already owned by uid ${currentOwner}; not reassigning.`);
}

await db.doc(`units/${UNIT_ID}`).set(
  {
    serial: UNIT_ID,
    model: MODEL,
    ...(ENGINE_TYPE ? { engineType: ENGINE_TYPE } : {}),
    firmwareVersion: '0.1.0-bench',
    ownerId: user.uid,
    theftFlag: false,
    regionTimezone: 'America/Denver',
    lastSeen: FieldValue.serverTimestamp(),
  },
  { merge: true },
);

console.log(`Unit ${UNIT_ID} bound to ${user.email}.`);
console.log(`Next: node scripts/seed-unit.mjs`);

process.exit(0);
