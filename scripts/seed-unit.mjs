// Seed / fake-telemetry script.
//
// Run from your laptop to exercise the full Firestore → app pipeline before
// the Pi is publishing. Overwrites units/{UNIT_ID}/current/snapshot every
// 3 s with realistic-looking fluctuating values.
//
// Usage:
//   1. Download a Firebase service-account JSON from
//      Firebase Console → Project settings → Service accounts → Generate new private key
//   2. Save it as ./scripts/service-account.json (gitignored)
//   3. node scripts/seed-unit.mjs            # uses default UNIT-001
//      UNIT_ID=UNIT-007 node scripts/seed-unit.mjs
//   4. Manually set ownerId on units/{UNIT_ID} in the Firebase Console
//      to your own auth uid so the app's security rules let you read it.

import { readFileSync } from 'node:fs';
import { initializeApp, cert } from 'firebase-admin/app';
import { getFirestore, FieldValue } from 'firebase-admin/firestore';

const UNIT_ID = process.env.UNIT_ID ?? 'UNIT-001';
const SA_PATH = process.env.SERVICE_ACCOUNT ?? './scripts/service-account.json';
const INTERVAL_MS = 3000;

const serviceAccount = JSON.parse(readFileSync(SA_PATH, 'utf-8'));
initializeApp({ credential: cert(serviceAccount) });
const db = getFirestore();

let soc = 78;
let voltage = 51.2;
let current = 8.4;
let temp = 28.5;

function jitter(value, delta) {
  return value + (Math.random() - 0.5) * delta;
}

function step() {
  soc = Math.max(8, Math.min(99, jitter(soc, 0.4)));
  voltage = jitter(48 + (soc - 50) * 0.08, 0.4);
  current = soc < 22 ? -jitter(14, 3) : jitter(8.4, 1.5);
  temp = jitter(temp, 0.2);

  const cellAvg = voltage / 16;
  const cells = Array.from({ length: 16 }, () => +(jitter(cellAvg, 0.012)).toFixed(3));
  const voltageMin = Math.min(...cells);
  const voltageMax = Math.max(...cells);

  const snapshot = {
    battery_soc: +soc.toFixed(1),
    battery_voltage: +voltage.toFixed(2),
    battery_current: +current.toFixed(2),
    battery_temp: +temp.toFixed(1),
    battery_power: +(voltage * current).toFixed(0),
    cells: {
      voltages: cells,
      voltageMin: +voltageMin.toFixed(3),
      voltageMax: +voltageMax.toFixed(3),
      voltageDelta: +(voltageMax - voltageMin).toFixed(3),
      tempMin: +(temp - 0.6).toFixed(1),
      tempMax: +(temp + 1.1).toFixed(1),
      tempAvg: +temp.toFixed(1),
    },
    cycleCount: 2418,
    bmsFaults: [],
    system_mode: current < 0 ? 'charging' : 'discharging',
    last_update: FieldValue.serverTimestamp(),
  };

  return db
    .doc(`units/${UNIT_ID}/current/snapshot`)
    .set(snapshot)
    .then(() => {
      const stamp = new Date().toISOString().slice(11, 19);
      process.stdout.write(
        `[${stamp}] ${UNIT_ID}  soc=${snapshot.battery_soc}%  v=${snapshot.battery_voltage}V  i=${snapshot.battery_current}A  p=${snapshot.battery_power}W\n`,
      );
    })
    .catch((err) => {
      console.error('write failed:', err.message);
    });
}

// Also make sure the parent unit doc exists so the security rules can read ownerId.
await db
  .doc(`units/${UNIT_ID}`)
  .set(
    {
      serial: UNIT_ID,
      model: 'SITEPULSE V1',
      firmwareVersion: '0.1.0-bench',
      theftFlag: false,
      regionTimezone: 'America/Denver',
      lastSeen: FieldValue.serverTimestamp(),
    },
    { merge: true },
  );

console.log(`Seeding ${UNIT_ID} every ${INTERVAL_MS}ms. Ctrl+C to stop.`);
console.log(`Reminder: set ownerId on units/${UNIT_ID} in the Firebase Console to your auth uid.`);

await step();
setInterval(step, INTERVAL_MS);
