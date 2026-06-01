import React, { useState } from 'react';
import { ActivityIndicator, ScrollView, StyleSheet, Text, View } from 'react-native';
import { CornerBrackets, Eyebrow, FigCaption, Screen, SecondaryCTA } from '../../components';
import { useUnitTelemetry } from '../../hooks/useUnitTelemetry';
import { useAuth } from '../../hooks/AuthContext';
import { toggleAc, wakeLcd } from '../../firebase/commands';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../../theme';

// Phase-1 dashboard. Subscribes to units/{unitId}/current/snapshot in
// Firestore. The Pi overwrites that doc every 2–5 s; we render whatever
// arrives and reflect staleness via the LIVE / STALE / OFFLINE eyebrow.
//
// Unit ID is hardcoded for now — replaced with the owner's claimed unit
// once the pair flow lands. Override via EXPO_PUBLIC_DEV_UNIT_ID for testing.

const DEV_UNIT_ID = process.env.EXPO_PUBLIC_DEV_UNIT_ID ?? 'UNIT-001';

const STALENESS_LABEL: Record<string, string> = {
  fresh: 'LIVE',
  stale: 'STALE',
  offline: 'OFFLINE',
  unknown: '—',
};

function fmt(value: number | undefined | null, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toFixed(digits);
}

export function DashboardScreen() {
  const { loading, snapshot, staleness, error } = useUnitTelemetry(DEV_UNIT_ID);
  const { user } = useAuth();
  // 'pending' covers the brief window between tap and Pi ack. We don't
  // round-trip the ack into here yet — just debounce by time to prevent
  // double-taps from queueing two presses back-to-back.
  const [wakeBusy, setWakeBusy] = useState(false);
  const [acBusy, setAcBusy] = useState(false);

  const onWakePress = async () => {
    if (!user || wakeBusy) return;
    setWakeBusy(true);
    try {
      await wakeLcd(DEV_UNIT_ID, user.uid);
    } catch (e) {
      // Surface via console; no in-screen error UI for a one-off bench
      // action. If this becomes user-facing, lift into a toast.
      console.warn('[dashboard] wakeLcd failed', e);
    } finally {
      // Leave the button disabled briefly so the servo has time to
      // physically complete its press-release cycle before the user can
      // queue another.
      setTimeout(() => setWakeBusy(false), 1200);
    }
  };

  const onAcPress = async () => {
    if (!user || acBusy) return;
    setAcBusy(true);
    try {
      await toggleAc(DEV_UNIT_ID, user.uid);
    } catch (e) {
      console.warn('[dashboard] toggleAc failed', e);
    } finally {
      setTimeout(() => setAcBusy(false), 1200);
    }
  };

  if (loading) {
    return (
      <Screen>
        <View style={styles.center}>
          <ActivityIndicator color={colors.textMuted} />
          <Text style={styles.connectLabel}>SUBSCRIBING  ·  {DEV_UNIT_ID}</Text>
        </View>
      </Screen>
    );
  }

  if (error) {
    return (
      <Screen>
        <View style={styles.center}>
          <Text style={[styles.connectLabel, { color: colors.danger }]}>
            FIRESTORE ERROR
          </Text>
          <Text style={styles.connectHint}>{error.message}</Text>
          <Text style={styles.connectHint}>UID  ·  {user?.uid ?? 'NONE'}</Text>
          <Text style={styles.connectHint}>UNIT  ·  {DEV_UNIT_ID}</Text>
        </View>
      </Screen>
    );
  }

  if (!snapshot) {
    return (
      <Screen>
        <View style={styles.center}>
          <Text style={styles.connectLabel}>NO TELEMETRY YET</Text>
          <Text style={styles.connectHint}>
            Waiting for the controller to publish its first snapshot.
          </Text>
          <Text style={styles.connectHint}>UID  ·  {user?.uid ?? 'NONE'}</Text>
          <Text style={styles.connectHint}>UNIT  ·  {DEV_UNIT_ID}</Text>
        </View>
      </Screen>
    );
  }

  const socStr = fmt(snapshot.battery_soc, 0);
  const stalenessKind =
    staleness === 'fresh' ? 'fresh' : staleness === 'stale' ? 'stale' : 'offline';

  return (
    <Screen>
      <ScrollView
        contentContainerStyle={styles.scroll}
        showsVerticalScrollIndicator={false}
      >
        <Eyebrow
          parts={[DEV_UNIT_ID, STALENESS_LABEL[staleness] ?? '—']}
          live
          staleness={stalenessKind}
        />

        <CornerBrackets style={styles.hero}>
          <View style={styles.heroInner}>
            <Text style={styles.heroNumeric}>{socStr}</Text>
            <Text style={styles.heroUnit}>%</Text>
          </View>
          <Text style={styles.heroSub}>
            STATE OF CHARGE  ·  {snapshot.output_mode.toUpperCase()}  ·  {fmt(snapshot.output_watts, 0)} W
          </Text>
        </CornerBrackets>

        <View style={styles.modeRow}>
          <Text style={styles.modeBadge}>{snapshot.system_mode.replace(/_/g, ' ').toUpperCase()}</Text>
          <Text style={styles.modeStamp}>
            {snapshot.last_update
              ? `UPDATED ${Math.max(0, Math.round((Date.now() - snapshot.last_update.toMillis()) / 1000))} s AGO`
              : 'NO TIMESTAMP'}
          </Text>
        </View>

        <View style={styles.metricGroup}>
          <Eyebrow parts={['Output power']} />
          <View style={styles.metricRow}>
            <Text style={styles.metricBig}>{fmt(snapshot.output_watts, 0)}</Text>
            <Text style={styles.metricUnit}>W</Text>
          </View>
          <Text style={styles.metricSub}>
            {snapshot.dc_active ? 'DC ●' : 'DC ○'}  ·  {snapshot.ac_active ? 'AC ●' : 'AC ○'}
          </Text>
        </View>

        <View style={styles.metricGroup}>
          <Eyebrow parts={['Time to empty']} />
          <View style={styles.metricRow}>
            <Text style={styles.metricBig}>
              {snapshot.time_to_empty_minutes === null || snapshot.time_to_empty_minutes === undefined
                ? '—'
                : `${Math.floor(snapshot.time_to_empty_minutes / 60)}:${String(snapshot.time_to_empty_minutes % 60).padStart(2, '0')}`}
            </Text>
            <Text style={styles.metricUnit}>h:mm</Text>
          </View>
          <Text style={styles.metricSub}>
            {snapshot.lcd_frame_rate_hz
              ? `LCD ${snapshot.lcd_frame_rate_hz.toFixed(1)} Hz`
              : 'LCD —'}
          </Text>
        </View>

        <View style={styles.actionGroup}>
          <SecondaryCTA
            label={wakeBusy ? 'Waking…' : 'Wake LCD'}
            onPress={onWakePress}
            disabled={wakeBusy || !user}
          />
          <SecondaryCTA
            label={acBusy ? 'Pressing…' : 'Aux'}
            onPress={onAcPress}
            disabled={acBusy || !user}
          />
        </View>

        <FigCaption number={1} label="Dashboard" detail={DEV_UNIT_ID} />
      </ScrollView>
    </Screen>
  );
}

const styles = StyleSheet.create({
  scroll: {
    paddingTop: spacing.md,
    paddingBottom: spacing.xxl,
    gap: spacing.xl,
  },
  center: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.md,
  },
  connectLabel: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  connectHint: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
    textAlign: 'center',
    paddingHorizontal: spacing.lg,
  },
  hero: {
    paddingVertical: spacing.xl,
    paddingHorizontal: spacing.md,
    marginHorizontal: spacing.xs,
  },
  heroInner: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    justifyContent: 'center',
    gap: spacing.xs,
  },
  heroNumeric: {
    color: colors.textDisplay,
    fontFamily: fonts.display,
    fontSize: 140,
    lineHeight: 140,
    letterSpacing: -4,
  },
  heroUnit: {
    color: colors.textMuted,
    fontFamily: fonts.display,
    fontSize: typeScale.titleLG,
    marginBottom: spacing.md,
  },
  heroSub: {
    marginTop: spacing.md,
    textAlign: 'center',
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  modeRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  modeBadge: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
    paddingVertical: spacing.xxs,
    paddingHorizontal: spacing.xs,
    borderWidth: hairline,
    borderColor: colors.borderStrong,
  },
  modeStamp: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  metricGroup: {
    gap: spacing.sm,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
    paddingTop: spacing.lg,
  },
  metricRow: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: spacing.xs,
  },
  metricBig: {
    color: colors.textDisplay,
    fontFamily: fonts.display,
    fontSize: typeScale.displayLG,
    lineHeight: typeScale.displayLG,
    letterSpacing: tracking.displayTight,
  },
  metricUnit: {
    color: colors.textMuted,
    fontFamily: fonts.display,
    fontSize: typeScale.titleLG,
    marginBottom: spacing.xs,
  },
  metricSub: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  actionGroup: {
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
    paddingTop: spacing.lg,
    gap: spacing.sm,
  },
});
