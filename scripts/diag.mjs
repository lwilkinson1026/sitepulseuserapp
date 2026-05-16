import { readFileSync } from 'node:fs';
import { initializeApp, cert } from 'firebase-admin/app';
import { getFirestore } from 'firebase-admin/firestore';

const sa = JSON.parse(readFileSync('./scripts/service-account.json', 'utf-8'));
initializeApp({ credential: cert(sa) });
const db = getFirestore();

const unit = await db.doc('units/UNIT-001').get();
console.log('units/UNIT-001 exists?', unit.exists);
console.log('data:', unit.data());

const snap = await db.doc('units/UNIT-001/current/snapshot').get();
console.log('\nunits/UNIT-001/current/snapshot exists?', snap.exists);
if (snap.exists) {
  const data = snap.data();
  console.log('soc:', data.battery_soc, 'last_update:', data.last_update?.toDate?.()?.toISOString());
}

process.exit(0);
