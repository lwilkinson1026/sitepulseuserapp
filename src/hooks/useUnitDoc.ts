// Generic Firestore document subscription, typed to the document shape.
//
// Used by feature screens to subscribe to a single config or current-state
// doc under units/{unitId}/...  Re-renders on every snapshot update.

import { useEffect, useState } from 'react';
import { doc, DocumentReference, onSnapshot } from 'firebase/firestore';
import { getFirebase } from '../firebase/config';

export type DocState<T> = {
  loading: boolean;
  data: T | null;
  error: Error | null;
};

/**
 * Subscribe to a document at `units/{unitId}/<...pathSegments>`.
 * Returns the current data plus loading and error flags. Re-renders
 * whenever Firestore pushes a new snapshot.
 *
 * Pass `unitId === null` to lazily skip the subscription (e.g. when
 * the user hasn't claimed a unit yet).
 */
export function useUnitDoc<T>(
  unitId: string | null,
  ...pathSegments: string[]
): DocState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState<boolean>(unitId !== null);
  const [error, setError] = useState<Error | null>(null);

  // useEffect deps include the joined path so any change re-subscribes.
  const path = pathSegments.join('/');
  // Which doc the current data/error belong to. When unitId changes (e.g.
  // null → the user's unit once ownership resolves), the render before the
  // effect runs would otherwise report loading=false with no data — callers
  // like RentalGate would briefly treat "not loaded yet" as "doc missing".
  const key = unitId ? `${unitId}/${path}` : null;
  const [loadedKey, setLoadedKey] = useState<string | null>(null);

  useEffect(() => {
    if (!unitId) {
      setData(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    const { db } = getFirebase();
    const ref = doc(db, 'units', unitId, ...pathSegments) as DocumentReference;
    const unsub = onSnapshot(
      ref,
      (snap) => {
        setData(snap.exists() ? (snap.data() as T) : null);
        setLoadedKey(key);
        setLoading(false);
      },
      (err) => {
        setError(err);
        setLoadedKey(key);
        setLoading(false);
      },
    );
    return unsub;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unitId, path]);

  return { loading: loading || (key !== null && loadedKey !== key), data, error };
}
