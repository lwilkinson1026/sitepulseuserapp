// Reverse-chronological subscription to units/{unitId}/events.
//
// Optional `kindFilter` narrows to a single event kind ('motion', etc.).
// Limited to `pageSize` most-recent docs (default 25) — Activity tab can
// pass a larger pageSize later if scroll-back is needed.

import { useEffect, useState } from 'react';
import {
  collection,
  limit,
  onSnapshot,
  orderBy,
  query,
  Timestamp,
  where,
} from 'firebase/firestore';
import { getFirebase } from '../firebase/config';
import type { EventDoc, EventKind } from '../firebase/types';

export type EventEntry = EventDoc & { id: string; at: Timestamp };

export type EventsState = {
  loading: boolean;
  events: EventEntry[];
  error: Error | null;
};

export function useUnitEvents(
  unitId: string | null,
  options: { kindFilter?: EventKind; pageSize?: number } = {},
): EventsState {
  const [events, setEvents] = useState<EventEntry[]>([]);
  const [loading, setLoading] = useState(unitId !== null);
  const [error, setError] = useState<Error | null>(null);
  const pageSize = options.pageSize ?? 25;
  const kindFilter = options.kindFilter;

  useEffect(() => {
    if (!unitId) {
      setEvents([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    const { db } = getFirebase();
    const col = collection(db, 'units', unitId, 'events');
    const q = kindFilter
      ? query(col, where('kind', '==', kindFilter), orderBy('at', 'desc'), limit(pageSize))
      : query(col, orderBy('at', 'desc'), limit(pageSize));
    const unsub = onSnapshot(
      q,
      (snap) => {
        const out: EventEntry[] = [];
        snap.forEach((doc) => {
          const data = doc.data() as EventDoc;
          // Skip docs whose server timestamp hasn't materialized yet —
          // they'll fire a follow-up snapshot once the write commits.
          if (!data.at) return;
          out.push({ ...data, id: doc.id, at: data.at as Timestamp });
        });
        setEvents(out);
        setLoading(false);
      },
      (err) => {
        setError(err);
        setLoading(false);
      },
    );
    return unsub;
  }, [unitId, kindFilter, pageSize]);

  return { loading, events, error };
}
