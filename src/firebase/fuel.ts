// Fuel dashboard writes.
//
// Unlike the command helpers in commands.ts, refuels and settings are written
// directly: refuels are a user-authored `fuel.refuel` event (the one event
// kind the security rules let the owner append), and settings live in the
// owner-writable units/{unitId}/fuel/settings doc. Neither touches hardware,
// so there's no Pi round-trip.

import {
  addDoc,
  collection,
  doc,
  serverTimestamp,
  setDoc,
} from 'firebase/firestore';
import { getFirebase } from './config';
import type {
  FuelRefuelMethod,
  FuelRefuelPayload,
  FuelSettings,
} from './types';
import { FUEL_DEFAULTS } from '../lib/fuelModel';

export const DEFAULT_FUEL_SETTINGS: FuelSettings = {
  tankGallons: FUEL_DEFAULTS.tankGallons,
  seedBurnRateGalPerHour: FUEL_DEFAULTS.seedBurnRateGalPerHour,
};

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

// Append a fuel.refuel event. `currentLevel` is the model's estimate of what
// was in the tank at the moment of the fill; we use it to compute the stored
// levelAfter anchor (full → capacity; add → current + poured).
export async function logRefuel(
  unitId: string,
  args: {
    method: FuelRefuelMethod;
    gallons: number | null;
    currentLevel: number;
    tankGallons: number;
  },
): Promise<string> {
  const { method, gallons, currentLevel, tankGallons } = args;
  const levelAfter =
    method === 'full'
      ? tankGallons
      : clamp(currentLevel + (gallons ?? 0), 0, tankGallons);

  const payload: FuelRefuelPayload = { method, gallons, levelAfter };

  const { db } = getFirebase();
  const ref = await addDoc(collection(db, 'units', unitId, 'events'), {
    kind: 'fuel.refuel',
    at: serverTimestamp(),
    source: 'app',
    payload,
  });
  return ref.id;
}

// Create or patch the fuel settings doc (tank size, price, burn-rate prior).
export async function updateFuelSettings(
  unitId: string,
  patch: Partial<FuelSettings>,
): Promise<void> {
  const { db } = getFirebase();
  await setDoc(doc(db, 'units', unitId, 'fuel', 'settings'), patch, {
    merge: true,
  });
}
