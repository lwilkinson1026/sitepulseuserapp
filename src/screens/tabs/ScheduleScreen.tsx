import React, { useMemo } from 'react';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  View,
} from 'react-native';
import { Eyebrow, FigCaption, Screen } from '../../components';
import { useUnitDoc } from '../../hooks/useUnitDoc';
import { useOptimistic } from '../../hooks/useOptimistic';
import { useAuth } from '../../hooks/AuthContext';
import { overrideEngine, updateChargeConfig } from '../../firebase/commands';
import type {
  ChargeConfig,
  ChargeWindow,
  EngineState,
} from '../../firebase/types';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../../theme';

// Phase C — engine recharge schedule.
//
// Reads config/charge + current/engine. Writes config changes via
// `charge.update` commands (Pi consumes, mirrors back). Engine override
// (run now / stop / allow quiet hours once) goes through `engine.override`.
//
// Custom windows aren't editable inline yet — the four presets overwrite
// the entire `windows` array. Tap a preset → it issues a charge.update with
// a complete config patch. The "Custom" chip is reserved for the future
// time-picker editor.

const DEV_UNIT_ID = process.env.EXPO_PUBLIC_DEV_UNIT_ID ?? 'UNIT-001';

const DAY_LABELS = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];

// ─── presets ────────────────────────────────────────────────────────────────
// Each preset is a complete config patch — applying one fully reconfigures
// the schedule, no merging required. Thresholds match the LiFePO4 14S
// defaults locked into the plan (25/85/12).

type PresetId = 'daytime_only' | 'eco' | 'quiet_off' | 'storm';

const PRESETS: Array<{
  id: PresetId;
  label: string;
  description: string;
  patch: Partial<ChargeConfig>;
}> = [
  {
    id: 'daytime_only',
    label: 'Daytime Only',
    description: 'Engine may run 08:00–18:00 daily. Solar carries the rest.',
    patch: {
      enabled: true,
      preset: 'daytime_only',
      windows: [{ start: '08:00', end: '18:00', weekdays: [0, 1, 2, 3, 4, 5, 6] }],
      socStart: 25,
      socStop: 85,
      socCritical: 12,
      quietHours: { start: '21:00', end: '07:00' },
      allowQuietOverride: false,
    },
  },
  {
    id: 'eco',
    label: 'Eco',
    description: 'Weekdays 09:00–17:00, narrower SoC band (30–80%) for cycle life.',
    patch: {
      enabled: true,
      preset: 'eco',
      windows: [{ start: '09:00', end: '17:00', weekdays: [1, 2, 3, 4, 5] }],
      socStart: 30,
      socStop: 80,
      socCritical: 12,
      quietHours: { start: '21:00', end: '07:00' },
      allowQuietOverride: false,
    },
  },
  {
    id: 'quiet_off',
    label: 'Quiet Hours Off',
    description: 'Engine never runs 21:00–07:00. Critical SoC still notifies.',
    patch: {
      enabled: true,
      preset: 'quiet_off',
      windows: [{ start: '07:00', end: '21:00', weekdays: [0, 1, 2, 3, 4, 5, 6] }],
      socStart: 25,
      socStop: 85,
      socCritical: 12,
      quietHours: { start: '21:00', end: '07:00' },
      allowQuietOverride: false,
    },
  },
  {
    id: 'storm',
    label: 'Storm Mode',
    description: 'Top the pack to 100%, run anytime. Quiet hours overridable on critical.',
    patch: {
      enabled: true,
      preset: 'storm',
      windows: [{ start: '00:00', end: '23:59', weekdays: [0, 1, 2, 3, 4, 5, 6] }],
      socStart: 50,
      socStop: 100,
      socCritical: 15,
      quietHours: { start: '23:00', end: '06:00' },
      allowQuietOverride: true,
    },
  },
];

const REASON_LABEL: Record<EngineState['reason'], string> = {
  soc_low: 'SOC LOW',
  soc_satisfied: 'SOC SATISFIED',
  window_closed: 'WINDOW CLOSED',
  quiet_hours: 'QUIET HOURS',
  disabled: 'DISABLED',
  manual_override: 'MANUAL OVERRIDE',
};

export function ScheduleScreen() {
  const { user } = useAuth();
  const chargeConfig = useUnitDoc<ChargeConfig>(DEV_UNIT_ID, 'config', 'charge');
  const engineState = useUnitDoc<EngineState>(DEV_UNIT_ID, 'current', 'engine');

  // Stable derived values + ALL hook calls up-front so hook order is
  // consistent across renders (React's rules-of-hooks). The early loading
  // return below must come AFTER every hook call on this screen.
  const cfg = chargeConfig.data;
  const eng = engineState.data;
  const windows: ChargeWindow[] = useMemo(() => cfg?.windows ?? [], [cfg]);

  // Optimistic state for every Firestore-bound control. Snaps the UI on tap,
  // syncs back when the Pi's ACK mirrors the new value into config/charge.
  const [enabled, setEnabledOptimistic] = useOptimistic(cfg?.enabled ?? false);
  const [activePreset, setActivePresetOptimistic] =
    useOptimistic<string | undefined>(cfg?.preset);
  const [allowQuietOverride, setAllowQuietOverrideOptimistic] =
    useOptimistic(cfg?.allowQuietOverride ?? false);

  const loading = chargeConfig.loading || engineState.loading;

  if (!user || loading) {
    return (
      <Screen>
        <View style={styles.center}>
          <ActivityIndicator color={colors.textMuted} />
          <Text style={styles.connectLabel}>LOADING SCHEDULE</Text>
        </View>
      </Screen>
    );
  }

  const desired = eng?.desired ?? 'idle';
  const reason = eng?.reason;
  const socStart = cfg?.socStart ?? 25;
  const socStop = cfg?.socStop ?? 85;
  const socCritical = cfg?.socCritical ?? 12;

  const onToggleEnabled = (next: boolean) => {
    setEnabledOptimistic(next);
    void updateChargeConfig(DEV_UNIT_ID, user.uid, { enabled: next });
  };

  const onApplyPreset = (presetId: PresetId) => {
    const preset = PRESETS.find((p) => p.id === presetId);
    if (!preset) return;
    setActivePresetOptimistic(presetId);
    if (preset.patch.enabled !== undefined) setEnabledOptimistic(preset.patch.enabled);
    if (preset.patch.allowQuietOverride !== undefined) {
      setAllowQuietOverrideOptimistic(preset.patch.allowQuietOverride);
    }
    void updateChargeConfig(DEV_UNIT_ID, user.uid, preset.patch);
  };

  const onToggleQuietOverride = (next: boolean) => {
    setAllowQuietOverrideOptimistic(next);
    void updateChargeConfig(DEV_UNIT_ID, user.uid, { allowQuietOverride: next });
  };

  const onRunNow = () => {
    void overrideEngine(DEV_UNIT_ID, user.uid, 'run_now');
  };

  const onStopEngine = () => {
    void overrideEngine(DEV_UNIT_ID, user.uid, 'stop');
  };

  return (
    <Screen>
      <ScrollView
        contentContainerStyle={styles.scroll}
        showsVerticalScrollIndicator={false}
      >
        <View style={styles.header}>
          <Eyebrow parts={['03 / Schedule', enabled ? 'enabled' : 'disabled']} />
          <Text style={styles.headline}>Engine{'\n'}schedule</Text>
        </View>

        {/* ── Master switch ─────────────────────────────────────────── */}
        <View style={styles.row}>
          <View style={{ flex: 1 }}>
            <Text style={styles.rowLabel}>Auto-recharge</Text>
            <Text style={styles.rowSubLabel}>
              {enabled
                ? 'SCHEDULER ARMED  ·  ENGINE WILL RUN PER WINDOWS'
                : 'SCHEDULER DISABLED  ·  NO AUTOMATIC ENGINE RUNS'}
            </Text>
          </View>
          <Switch
            value={enabled}
            onValueChange={onToggleEnabled}
            trackColor={{ false: colors.borderStrong, true: colors.textDisplay }}
            thumbColor={enabled ? colors.background : colors.textMuted}
            ios_backgroundColor={colors.borderStrong}
          />
        </View>

        {/* ── Live engine state ─────────────────────────────────────── */}
        <View style={[styles.engineCard, desired === 'run' ? styles.engineCardRun : null]}>
          <View style={styles.engineRow}>
            <Text style={styles.engineLabel}>ENGINE</Text>
            <Text style={styles.engineState}>
              {desired === 'run' ? 'RUNNING' : 'IDLE'}
            </Text>
          </View>
          {reason ? (
            <Text style={styles.engineReason}>
              {REASON_LABEL[reason] ?? reason.toUpperCase()}
              {eng && 'socAtEval' in (eng as object)
                ? `  ·  SOC ${((eng as unknown as { socAtEval: number | null }).socAtEval ?? 0).toFixed(0)}%`
                : ''}
            </Text>
          ) : null}

          <View style={styles.engineActions}>
            <Pressable
              onPress={onRunNow}
              disabled={desired === 'run'}
              style={[
                styles.engineButton,
                desired === 'run' ? styles.engineButtonDisabled : null,
              ]}
            >
              <Text style={styles.engineButtonLabel}>RUN NOW</Text>
            </Pressable>
            <Pressable
              onPress={onStopEngine}
              disabled={desired === 'idle'}
              style={[
                styles.engineButton,
                desired === 'idle' ? styles.engineButtonDisabled : null,
              ]}
            >
              <Text style={styles.engineButtonLabel}>STOP</Text>
            </Pressable>
          </View>
        </View>

        {/* ── Preset chips ──────────────────────────────────────────── */}
        <View style={styles.section}>
          <Eyebrow parts={['Presets']} />
          {PRESETS.map((preset) => {
            const active = activePreset === preset.id;
            return (
              <Pressable
                key={preset.id}
                onPress={() => onApplyPreset(preset.id)}
                style={[styles.preset, active ? styles.presetActive : null]}
              >
                <View style={styles.presetHeader}>
                  <Text
                    style={[styles.presetLabel, active ? styles.presetLabelActive : null]}
                  >
                    {preset.label}
                  </Text>
                  {active ? <Text style={styles.presetActiveBadge}>ACTIVE</Text> : null}
                </View>
                <Text style={styles.presetDescription}>{preset.description}</Text>
              </Pressable>
            );
          })}
        </View>

        {/* ── Window summary ────────────────────────────────────────── */}
        <View style={styles.section}>
          <Eyebrow parts={['Allowed windows', `${windows.length} configured`]} />
          {windows.length === 0 ? (
            <Text style={styles.rowSubLabel}>NO WINDOWS — APPLY A PRESET TO BEGIN</Text>
          ) : (
            windows.map((w, i) => (
              <View key={`${w.start}-${w.end}-${i}`} style={styles.windowRow}>
                <View style={styles.windowTime}>
                  <Text style={styles.windowTimeText}>
                    {w.start} — {w.end}
                  </Text>
                </View>
                <View style={styles.windowDays}>
                  {DAY_LABELS.map((d, di) => (
                    <Text
                      key={d}
                      style={[
                        styles.windowDayLabel,
                        w.weekdays?.includes(di) ? styles.windowDayLabelActive : null,
                      ]}
                    >
                      {d}
                    </Text>
                  ))}
                </View>
              </View>
            ))
          )}
        </View>

        {/* ── Thresholds (read-only summary for v1) ─────────────────── */}
        <View style={styles.section}>
          <Eyebrow parts={['Thresholds', 'LiFePO4 14S']} />
          <View style={styles.thresholdRow}>
            <Text style={styles.thresholdKey}>ENGINE STARTS BELOW</Text>
            <Text style={styles.thresholdValue}>{socStart}%</Text>
          </View>
          <View style={styles.thresholdRow}>
            <Text style={styles.thresholdKey}>ENGINE STOPS ABOVE</Text>
            <Text style={styles.thresholdValue}>{socStop}%</Text>
          </View>
          <View style={styles.thresholdRow}>
            <Text style={styles.thresholdKey}>NOTIFY AT</Text>
            <Text style={styles.thresholdValue}>{socCritical}%</Text>
          </View>
        </View>

        {/* ── Quiet override ────────────────────────────────────────── */}
        <View style={styles.row}>
          <View style={{ flex: 1 }}>
            <Text style={styles.rowLabel}>Override quiet hours on critical SoC</Text>
            <Text style={styles.rowSubLabel}>
              {allowQuietOverride
                ? 'ENGINE MAY RUN OVERNIGHT IF BATTERY HITS CRITICAL'
                : 'NOTIFICATION ONLY  ·  TAP TO ALLOW ONE-SHOT OVERRIDE'}
            </Text>
          </View>
          <Switch
            value={allowQuietOverride}
            onValueChange={onToggleQuietOverride}
            trackColor={{ false: colors.borderStrong, true: colors.textDisplay }}
            thumbColor={allowQuietOverride ? colors.background : colors.textMuted}
            ios_backgroundColor={colors.borderStrong}
          />
        </View>

        <FigCaption number={3} label="Schedule" detail={DEV_UNIT_ID} />
      </ScrollView>
    </Screen>
  );
}

const styles = StyleSheet.create({
  scroll: {
    paddingTop: spacing.md,
    paddingBottom: spacing.xxl,
    gap: spacing.lg,
  },
  center: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.md,
  },
  connectLabel: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  header: {
    paddingBottom: spacing.sm,
  },
  headline: {
    marginTop: spacing.md,
    color: colors.textDisplay,
    fontFamily: fonts.display,
    fontSize: typeScale.displayMD,
    lineHeight: typeScale.displayMD,
    letterSpacing: tracking.displayTight,
  },

  section: {
    gap: spacing.md,
    paddingTop: spacing.md,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.md,
    paddingVertical: spacing.sm,
  },
  rowLabel: {
    color: colors.textDisplay,
    fontFamily: fonts.bodyMedium,
    fontSize: typeScale.bodyLG,
  },
  rowSubLabel: {
    marginTop: 2,
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },

  engineCard: {
    borderWidth: hairline,
    borderColor: colors.borderStrong,
    padding: spacing.lg,
    gap: spacing.sm,
  },
  engineCardRun: {
    borderColor: colors.textDisplay,
    backgroundColor: colors.surface,
  },
  engineRow: {
    flexDirection: 'row',
    alignItems: 'baseline',
    justifyContent: 'space-between',
  },
  engineLabel: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  engineState: {
    color: colors.textDisplay,
    fontFamily: fonts.display,
    fontSize: typeScale.titleLG,
    letterSpacing: tracking.displayTight,
  },
  engineReason: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  engineActions: {
    flexDirection: 'row',
    gap: spacing.sm,
    marginTop: spacing.sm,
  },
  engineButton: {
    flex: 1,
    paddingVertical: spacing.sm,
    alignItems: 'center',
    borderWidth: hairline,
    borderColor: colors.borderStrong,
  },
  engineButtonDisabled: {
    opacity: 0.35,
  },
  engineButtonLabel: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },

  preset: {
    paddingVertical: spacing.md,
    paddingHorizontal: spacing.md,
    borderWidth: hairline,
    borderColor: colors.borderStrong,
    gap: spacing.xs,
  },
  presetActive: {
    borderColor: colors.textDisplay,
    backgroundColor: colors.surface,
  },
  presetHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'baseline',
  },
  presetLabel: {
    color: colors.textBody,
    fontFamily: fonts.bodyMedium,
    fontSize: typeScale.bodyLG,
  },
  presetLabelActive: {
    color: colors.textDisplay,
  },
  presetActiveBadge: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  presetDescription: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },

  windowRow: {
    gap: spacing.xs,
    paddingVertical: spacing.sm,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
  },
  windowTime: {},
  windowTimeText: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  windowDays: {
    flexDirection: 'row',
    gap: spacing.sm,
  },
  windowDayLabel: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  windowDayLabelActive: {
    color: colors.textDisplay,
  },

  thresholdRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'baseline',
    paddingVertical: spacing.xs,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
  },
  thresholdKey: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  thresholdValue: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
});
