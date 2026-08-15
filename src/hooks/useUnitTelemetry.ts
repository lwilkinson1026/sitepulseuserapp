import { useEffect, useState } from 'react';
import { doc, onSnapshot, Timestamp } from 'firebase/firestore';
import { getFirebase } from '../firebase/config';
import type { TelemetrySnapshot } from '../firebase/types';

// Real-time subscription to units/{unitId}/current/snapshot.
// Returns the latest telemetry plus a staleness signal driven by `last_update`.
//
// Staleness thresholds are derived from the Pi's publish cadence
// (SITEPULSE_INTERVAL in pi/firebase_publisher.py), NOT from absolute time.
// The original spec §10.3 values — 15 s / 60 s — assumed the 3 s cadence the
// publisher shipped with. On 2026-08-15 that moved to 15 s to stay inside the
// Firestore free-tier write quota, which put every healthy unit permanently
// on the 15 s boundary and flickered the dot amber.
//
// Keep these as multiples of the publish interval so the two stay coupled:
//   fresh  : < 3 intervals
//   stale  : 3-10 intervals (amber)
//   offline: > 10 intervals (red)
//
// If the Pi cadence changes again, change PUBLISH_INTERVAL_MS here to match.

export type Staleness = 'fresh' | 'stale' | 'offline' | 'unknown';

export type UnitTelemetryState = {
  loading: boolean;
  snapshot: TelemetrySnapshot | null;
  staleness: Staleness;
  error: Error | null;
};

const PUBLISH_INTERVAL_MS = 15_000;
const STALE_AFTER_MS = PUBLISH_INTERVAL_MS * 3;    // 45 s
const OFFLINE_AFTER_MS = PUBLISH_INTERVAL_MS * 10; // 150 s

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
