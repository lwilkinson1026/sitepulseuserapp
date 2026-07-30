// Resolves WHICH unit the signed-in user is looking at.
//
// Before this existed, every screen read a build-time constant:
//
//   const DEV_UNIT_ID = process.env.EXPO_PUBLIC_DEV_UNIT_ID ?? 'UNIT-001';
//
// which meant the deployed app always asked for UNIT-001 regardless of who
// signed in. UNIT-001 belongs to a different account than UNIT-002, so the
// security rules correctly refused the read and the dashboard rendered
// "FIRESTORE ERROR — Missing or insufficient permissions". The bug was never
// in the rules or the Pi; the app was simply asking for someone else's unit.
//
// Ownership is now the authoritative source, resolved live from Firestore.
// EXPO_PUBLIC_DEV_UNIT_ID survives only as a *preference among units the user
// actually owns* (see PINNED_UNIT_ID below) — it can no longer point the app
// at a unit the signed-in user has no access to.

import React, {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { collection, onSnapshot, query, where } from 'firebase/firestore';
import { getFirebase } from '../firebase/config';
import type { UnitDoc } from '../firebase/types';
import { useAuth } from './AuthContext';

/**
 * Optional bench convenience: when an admin owns more than one unit, pin which
 * one opens by default. Treated as a filter over owned units, never as an
 * override — if the signed-in user doesn't own it, it is ignored.
 *
 * Trimmed and emptiness-checked rather than defaulted with `??`, because `??`
 * only falls back on null/undefined. An env var set to "" in the hosting
 * provider is a string, passes `??`, and would silently become the pin.
 */
const PINNED_UNIT_ID = (process.env.EXPO_PUBLIC_DEV_UNIT_ID ?? '').trim() || null;

export type ActiveUnitState = {
  /** True until the first Firestore response (or immediately false if signed out). */
  loading: boolean;
  /** The resolved unit id, or null when signed out / no unit owned / errored. */
  unitId: string | null;
  /** The resolved unit document, for cellCount, timezone, serial, theftFlag, etc. */
  unit: UnitDoc | null;
  /** Every unit this user owns, for a future unit-switcher. */
  ownedUnitIds: string[];
  /**
   * True when the user is signed in, the query succeeded, and it came back
   * empty. Distinct from `loading` and from `error` — this is the
   * "you haven't claimed a generator yet" state, which wants an onboarding
   * prompt rather than an error message.
   */
  hasNoUnits: boolean;
  error: Error | null;
};

const EMPTY: ActiveUnitState = {
  loading: false,
  unitId: null,
  unit: null,
  ownedUnitIds: [],
  hasNoUnits: false,
  error: null,
};

const ActiveUnitContext = createContext<ActiveUnitState | undefined>(undefined);

export function ActiveUnitProvider({ children }: { children: React.ReactNode }) {
  const { user, initializing } = useAuth();
  const uid = user?.uid ?? null;

  const [state, setState] = useState<ActiveUnitState>({ ...EMPTY, loading: true });

  useEffect(() => {
    // Wait for auth to settle before deciding there is no user, otherwise the
    // first paint after a reload reports "no units" for the split second
    // before onAuthStateChanged fires.
    if (initializing) {
      setState({ ...EMPTY, loading: true });
      return;
    }
    if (!uid) {
      setState(EMPTY);
      return;
    }

    setState({ ...EMPTY, loading: true });

    const { db } = getFirebase();
    // Subscribed rather than fetched once so claiming a unit (or having one
    // released) reflects without a reload. The where() clause is also what
    // makes this readable at all: the rules allow a list of /units only when
    // the query filters on ownerId, so the server can prove no other user's
    // unit can come back.
    const q = query(collection(db, 'units'), where('ownerId', '==', uid));

    const unsub = onSnapshot(
      q,
      (snap) => {
        const docs = snap.docs.map((d) => ({ id: d.id, data: d.data() as UnitDoc }));
        const ownedUnitIds = docs.map((d) => d.id);

        // Pin wins only if owned; otherwise first owned unit. Sorted so the
        // default is stable across snapshots instead of depending on the
        // order Firestore happens to return.
        ownedUnitIds.sort();
        const chosenId =
          (PINNED_UNIT_ID && ownedUnitIds.includes(PINNED_UNIT_ID)
            ? PINNED_UNIT_ID
            : ownedUnitIds[0]) ?? null;
        const chosen = docs.find((d) => d.id === chosenId) ?? null;

        setState({
          loading: false,
          unitId: chosenId,
          unit: chosen?.data ?? null,
          ownedUnitIds,
          hasNoUnits: ownedUnitIds.length === 0,
          error: null,
        });
      },
      (err) => {
        setState({ ...EMPTY, error: err as Error });
      },
    );
    return unsub;
  }, [uid, initializing]);

  return (
    <ActiveUnitContext.Provider value={state}>{children}</ActiveUnitContext.Provider>
  );
}

export function useActiveUnit(): ActiveUnitState {
  const ctx = useContext(ActiveUnitContext);
  if (!ctx) {
    throw new Error('useActiveUnit must be used inside <ActiveUnitProvider>');
  }
  return ctx;
}

/**
 * Convenience for the many screens that only need the id and are already
 * happy to pass `null` down to useUnitDoc / useUnitTelemetry, both of which
 * skip their subscription on null.
 */
export function useActiveUnitId(): string | null {
  return useActiveUnit().unitId;
}
