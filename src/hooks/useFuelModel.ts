// Derives the fuel dashboard model from the event stream + settings doc.
//
// Reads the same append-only event log the Activity tab shows (engine
// start/stop for runtime, fuel.refuel for anchors) plus the owner-writable
// fuel/settings doc, and folds them through the pure model in lib/fuelModel.
// A slow ticker re-evaluates so the level / runway tick down over time even
// when no new event arrives.

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useUnitEvents } from './useUnitEvents';
import { useUnitDoc } from './useUnitDoc';
import { DEFAULT_FUEL_SETTINGS, logRefuel } from '../firebase/fuel';
import {
  computeFuelModel,
  type FuelModel,
  type RawFuelEvent,
} from '../lib/fuelModel';
import type { FuelRefuelMethod, FuelSettings } from '../firebase/types';

// Pull enough history to cover the trailing window + several refuel cycles.
// Filtered client-side so no composite (kind + at) index is required.
const EVENT_PAGE_SIZE = 500;
const RELEVANT_KINDS = new Set(['engine.start', 'engine.stop', 'fuel.refuel']);
const TICK_MS = 30_000;

export interface UseFuelModel {
  loading: boolean;
  model: FuelModel;
  settings: FuelSettings;
  error: Error | null;
  refuel: (method: FuelRefuelMethod, gallons: number | null) => Promise<void>;
}

export function useFuelModel(unitId: string | null): UseFuelModel {
  const events = useUnitEvents(unitId, { pageSize: EVENT_PAGE_SIZE });
  const settingsDoc = useUnitDoc<FuelSettings>(
    unitId,
    'fuel',
    'settings',
  );
  const settings: FuelSettings = settingsDoc.data ?? DEFAULT_FUEL_SETTINGS;

  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, []);

  const rawEvents: RawFuelEvent[] = useMemo(
    () =>
      events.events
        .filter((e) => RELEVANT_KINDS.has(e.kind))
        .map((e) => ({
          kind: e.kind,
          atMs: e.at.toMillis(),
          payload: (e.payload ?? {}) as Record<string, unknown>,
        })),
    [events.events],
  );

  const model = useMemo(
    () => computeFuelModel(rawEvents, settings, nowMs),
    [rawEvents, settings, nowMs],
  );

  const refuel = useCallback(
    async (method: FuelRefuelMethod, gallons: number | null) => {
      if (!unitId) return;
      await logRefuel(unitId, {
        method,
        gallons,
        currentLevel: model.level ?? 0,
        tankGallons: settings.tankGallons,
      });
    },
    [unitId, model.level, settings.tankGallons],
  );

  return {
    loading: events.loading || settingsDoc.loading,
    model,
    settings,
    error: events.error ?? settingsDoc.error,
    refuel,
  };
}
