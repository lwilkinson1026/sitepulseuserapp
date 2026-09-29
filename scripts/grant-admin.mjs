// Grant (or revoke) SitePulse fleet-admin access on an app account.
//
// Admins carry the `admin: true` custom claim. The security rules let them
// read and command every unit, and the app shows them a unit switcher. Only
// the Admin SDK can set a custom claim, so a client can never grant itself.
//
// Usage:
//   ADMIN_EMAIL=landonw1@mac.com node scripts/grant-admin.mjs
//   ADMIN_EMAIL=landonw1@mac.com REVOKE=1 node scripts/grant-admin.mjs
//
// Optional:
//   PUSH=1   also add the account to fleet/admins.pushUids, so it gets push
//            alerts for every unit (REVOKE=1 removes it again)
//
// Takes effect on the account's next sign-in or ID-token refresh; the app
// force-refreshes the token at startup, so a reload is enough.
//
// Never grant this to a login a customer can use — they would see the whole
// fleet.
//
// Firestore/Auth: uses the service-account file if present, otherwise your
// own Google login via Application Default Credentials — run
// `gcloud auth application-default login` once first.

import { existsSync, readFileSync } from 'node:fs';
import { initializeApp, cert } from 'firebase-admin/app';
import { getAuth } from 'firebase-admin/auth';
import { getFirestore, FieldValue } from 'firebase-admin/firestore';

const EMAIL = (process.env.ADMIN_EMAIL ?? '').trim();
const REVOKE = process.env.REVOKE === '1';
const PUSH = process.env.PUSH === '1';
const SA_PATH = process.env.SERVICE_ACCOUNT ?? './scripts/service-account.json';

if (!EMAIL) throw new Error('ADMIN_EMAIL is required.');

if (existsSync(SA_PATH)) {
  initializeApp({ credential: cert(JSON.parse(readFileSync(SA_PATH, 'utf-8'))) });
} else {
  initializeApp({ projectId: process.env.FIREBASE_PROJECT ?? 'sitepulse-userapp' });
}

const auth = getAuth();
const user = await auth.getUserByEmail(EMAIL);

// Merge rather than overwrite, so other claims survive.
const claims = { ...(user.customClaims ?? {}) };
if (REVOKE) delete claims.admin;
else claims.admin = true;
await auth.setCustomUserClaims(user.uid, claims);
console.log(`${REVOKE ? 'Revoked' : 'Granted'} admin: ${user.email} (${user.uid})`);

if (PUSH || REVOKE) {
  await getFirestore()
    .doc('fleet/admins')
    .set(
      { pushUids: REVOKE ? FieldValue.arrayRemove(user.uid) : FieldValue.arrayUnion(user.uid) },
      { merge: true },
    );
  console.log(`${REVOKE ? 'Removed from' : 'Added to'} fleet/admins.pushUids`);
}
process.exit(0);
