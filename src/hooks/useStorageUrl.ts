// Resolve a Firebase Storage path (e.g. units/UNIT-001/clips/123.mp4) to
// a temporary download URL the <video>/<Image> tag can fetch.
//
// Returns null while loading or if the path is null/empty. Storage rules
// enforce ownership — the URL only works for signed-in owners of the unit.

import { useEffect, useState } from 'react';
import { getDownloadURL, ref } from 'firebase/storage';
import { getFirebase } from '../firebase/config';

export function useStorageUrl(path: string | null | undefined): {
  url: string | null;
  loading: boolean;
  error: Error | null;
} {
  const [url, setUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(!!path);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (!path) {
      setUrl(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);

    (async () => {
      try {
        const { storage } = getFirebase();
        const r = ref(storage, path);
        const downloadUrl = await getDownloadURL(r);
        if (!cancelled) {
          setUrl(downloadUrl);
          setLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err as Error);
          setLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [path]);

  return { url, loading, error };
}
