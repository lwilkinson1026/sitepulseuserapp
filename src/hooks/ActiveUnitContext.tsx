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
//
// Fleet admins (the `admin: true` claim — see AuthContext) are the exception:
// they list every unit and can switch between them with setUnitId(). The
// choice is remembered per device so a restart reopens the same unit.

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { collection, onSnapshot, query, where } from 'firebase/firestore';
import { getFirebase } from '../firebase/config';
import type { UnitDoc } from '../firebase/types';
import { useAuth } from './AuthContext';

/**
 * Optional bench convenience: when an admin owns more than one unit, pin which
 * one opens by default. Treated as a filter over accessible units, never as an
 * override — if the signed-in user can't open it, it is ignored.
 *
 * Trimmed and emptiness-checked rather than defaulted with `??`, because `??`
 * only falls back on null/undefined. An env var set to "" in the hosting
 * provider is a string, passes `??`, and would silently become the pin.
 */
const PINNED_UNIT_ID = (process.env.EXPO_PUBLIC_DEV_UNIT_ID ?? '').trim() || null;

/** Where the unit switcher remembers the last pick on this device. */
const SELECTED_UNIT_KEY = 'sitepulse.selectedUnitId';

export type ActiveUnitState = {
  /** True until the first Firestore response (or immediately false if signed out). */
  loading: boolean;
  /** The resolved unit id, or null when signed out / no unit owned / errored. */
  unitId: string | null;
  /** The resolved unit document, for cellCount, timezone, serial, theftFlag, etc. */
  unit: UnitDoc | null;
  /**
   * Every unit this user can open: the ones they own, or — for a fleet
   * admin — every unit. Sorted. More than one means show the switcher.
   */
  unitIds: string[];
  /**
   * True when the signed-in user owns the active unit. False for an admin
   * looking at a customer's unit — the UI uses it to keep payer-only actions
   * (Stripe checkout / billing portal) away from staff.
   */
  isOwner: boolean;
  /** Switch the active unit. Ignored for ids not in `unitIds`. */
  setUnitId: (id: string) => void;
  /**
   * True when the user is signed in, the query succeeded, and it came back
   * empty. Distinct from `loading` and from `error` — this is the
   * "you haven't claimed a generator yet" state, which wants an onboarding
   * prompt rather than an error message.
   */
  hasNoUnits: boolean;
  error: Error | null;
};

type UnitsState = {
  loading: boolean;
  docs: { id: string; data: UnitDoc }[];
  error: Error | null;
};

const NO_UNITS: UnitsState = { loading: false, docs: [], error: null };

const ActiveUnitContext = createContext<ActiveUnitState | undefined>(undefined);

export function ActiveUnitProvider({ children }: { children: React.ReactNode }) {
  const { user, initializing, isAdmin } = useAuth();
  const uid = user?.uid ?? null;

  const [units, setUnits] = useState<UnitsState>({ ...NO_UNITS, loading: true });
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Restore the last pick. Best effort: storage can be unavailable (private
  // browsing), and then the default choice below applies.
  useEffect(() => {
    AsyncStorage.getItem(SELECTED_UNIT_KEY)
      .then((id) => {
        if (id) setSelectedId((cur) => cur ?? id);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    // Wait for auth to settle before deciding there is no user, otherwise the
    // first paint after a reload reports "no units" for the split second
    // before onAuthStateChanged fires.
    if (initializing) {
      setUnits({ ...NO_UNITS, loading: true });
      return;
    }
    if (!uid) {
      setUnits(NO_UNITS);
      return;
    }

    setUnits({ ...NO_UNITS, loading: true });

    const { db } = getFirebase();
    // Subscribed rather than fetched once so claiming a unit (or having one
    // released) reflects without a reload. For customers the where() clause
    // is also what makes this readable at all: the rules allow a list of
    // /units only when the query filters on ownerId, so the server can prove
    // no other user's unit can come back. Admins may list the whole fleet.
    const q = isAdmin
      ? query(collection(db, 'units'))
      : query(collection(db, 'units'), where('ownerId', '==', uid));

    const unsub = onSnapshot(
      q,
      (snap) => {
        const docs = snap.docs
          .map((d) => ({ id: d.id, data: d.data() as UnitDoc }))
          // Sorted so the default is stable across snapshots instead of
          // depending on the order Firestore happens to return.
          .sort((a, b) => a.id.localeCompare(b.id));
        setUnits({ loading: false, docs, error: null });
      },
      (err) => {
        setUnits({ ...NO_UNITS, error: err as Error });
      },
    );
    return unsub;
  }, [uid, initializing, isAdmin]);

  const setUnitId = useCallback((id: string) => {
    setSelectedId(id);
    AsyncStorage.setItem(SELECTED_UNIT_KEY, id).catch(() => {});
  }, []);

  const state = useMemo<ActiveUnitState>(() => {
    const unitIds = units.docs.map((d) => d.id);
    // Explicit pick, then the build-time pin, then the first unit — each only
    // if this user can actually open it.
    const chosenId =
      [selectedId, PINNED_UNIT_ID].find((id) => id && unitIds.includes(id)) ??
      unitIds[0] ??
      null;
    const chosen = units.docs.find((d) => d.id === chosenId) ?? null;
    return {
      loading: units.loading,
      unitId: chosenId,
      unit: chosen?.data ?? null,
      unitIds,
      isOwner: !!chosen && !!uid && chosen.data.ownerId === uid,
      setUnitId,
      hasNoUnits: !units.loading && !units.error && !!uid && unitIds.length === 0,
      error: units.error,
    };
  }, [units, selectedId, uid, setUnitId]);

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
