// Optimistic value with source-of-truth sync.
//
// Returns [displayValue, setOptimistic] where:
//   - displayValue is the optimistic value if one's been set, otherwise truthValue.
//   - setOptimistic snaps the display to the new value immediately.
//   - When truthValue changes to match optimistic, optimistic clears and the
//     hook reverts to mirroring truthValue. This means a Firestore round-trip
//     that confirms our intent feels seamless; one that rejects (truthValue
//     comes back different) snaps the UI back to reality.
//
// Use case: every toggle in this app writes a command to Firestore that the
// Pi consumes and ACKs by mirroring state back. Without optimistic state,
// the toggle would appear dead until the Pi responds (or forever, if it's
// offline). With it, the UI feels native.

import { useEffect, useRef, useState } from 'react';

export function useOptimistic<T>(truthValue: T): [T, (next: T) => void] {
  const [optimistic, setOptimistic] = useState<{ value: T } | null>(null);
  const lastTruth = useRef(truthValue);

  // If truth has changed since we set optimistic, decide whether to clear.
  // Clearing on every truth change keeps the hook honest: as soon as
  // Firestore's source-of-truth weighs in, drop the override.
  useEffect(() => {
    if (lastTruth.current !== truthValue) {
      lastTruth.current = truthValue;
      setOptimistic(null);
    }
  }, [truthValue]);

  const display = optimistic ? optimistic.value : truthValue;

  const set = (next: T) => {
    lastTruth.current = truthValue;
    setOptimistic({ value: next });
  };

  return [display, set];
}
