import { useEffect, useState } from 'react';
import { doc, onSnapshot, Timestamp } from 'firebase/firestore';
import { getFirebase } from '../firebase/config';
import type { TelemetrySnapshot } from '../firebase/types';

// Real-time subscription to units/{unitId}/current/snapshot.
// Returns the latest telemetry plus a staleness signal driven by `last_update`.
//
// Staleness thresholds match spec §10.3 — live dot:
//   fresh  : updated < 15 s ago
//   stale  : 15–60 s ago (amber)
//   offline: > 60 s ago   (red)

export type Staleness = 'fresh' | 'stale' | 'offline' | 'unknown';

export type UnitTelemetryState = {
  loading: boolean;
  snapshot: TelemetrySnapshot | null;
  staleness: Staleness;
  error: Error | null;
};

const STALE_AFTER_MS = 15_000;
const OFFLINE_AFTER_MS = 60_000;

function ageMs(ts: Timestamp | null | undefined): number | null {
  if (!ts) return null;
  return Date.now() - ts.toMillis();
}

function classify(ts: Timestamp | null | undefined): Staleness {
  const age = ageMs(ts);
  if (age === null) return 'unknown';
  if (age > OFFLINE_AFTER_MS) return 'offline';
  if (age > STALE_AFTER_MS) return 'stale';
  return 'fresh';
}

export function useUnitTelemetry(unitId: string | null): UnitTelemetryState {
  const [snapshot, setSnapshot] = useState<TelemetrySnapshot | null>(null);
  const [loading, setLoading] = useState<boolean>(unitId !== null);
  const [error, setError] = useState<Error | null>(null);
  const [staleness, setStaleness] = useState<Staleness>('unknown');

  // Firestore listener — re-establishes when unitId changes.
  useEffect(() => {
    if (!unitId) {
      setSnapshot(null);
      setLoading(false);
      setStaleness('unknown');
      return;
    }
    setLoading(true);
    const { db } = getFirebase();
    const ref = doc(db, 'units', unitId, 'current', 'snapshot');
    const unsub = onSnapshot(
      ref,
      (snap) => {
        if (!snap.exists()) {
          setSnapshot(null);
          setStaleness('unknown');
        } else {
          const data = snap.data() as TelemetrySnapshot;
          setSnapshot(data);
          setStaleness(classify(data.last_update));
        }
        setLoading(false);
      },
      (err) => {
        setError(err);
        setLoading(false);
      },
    );
    return unsub;
  }, [unitId]);

  // Tick every 5 s to re-evaluate staleness even when no new doc arrives.
  // Cheap — re-render only fires if the bucket actually changes.
  useEffect(() => {
    if (!snapshot) return;
    const interval = setInterval(() => {
      setStaleness((prev) => {
        const next = classify(snapshot.last_update);
        return next === prev ? prev : next;
      });
    }, 5_000);
    return () => clearInterval(interval);
  }, [snapshot]);

  return { loading, snapshot, staleness, error };
}
