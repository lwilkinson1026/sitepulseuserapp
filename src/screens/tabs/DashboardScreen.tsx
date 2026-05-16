import React from 'react';
import { ActivityIndicator, ScrollView, StyleSheet, Text, View } from 'react-native';
import { CornerBrackets, Eyebrow, FigCaption, Screen } from '../../components';
import { useUnitTelemetry } from '../../hooks/useUnitTelemetry';
import { useAuth } from '../../hooks/AuthContext';
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
            STATE OF CHARGE  ·  {fmt(snapshot.battery_voltage, 1)} V  ·  {fmt(snapshot.battery_current, 1)} A
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

        {snapshot.cells ? (
          <View style={styles.metricGroup}>
            <Eyebrow parts={['Cells', `${snapshot.cells.voltages.length} × LiFePO4`, `Δ ${fmt(snapshot.cells.voltageDelta, 3)} V`]} />
            <View style={styles.metricRow}>
              <Text style={styles.metricBig}>{fmt(snapshot.cells.voltageMin, 3)}</Text>
              <Text style={styles.metricUnit}>V min</Text>
            </View>
            <Text style={styles.metricSub}>
              MAX {fmt(snapshot.cells.voltageMax, 3)} V  ·  TEMP {fmt(snapshot.cells.tempAvg, 1)} °C
            </Text>
          </View>
        ) : null}

        <View style={styles.metricGroup}>
          <Eyebrow parts={['Battery power']} />
          <View style={styles.metricRow}>
            <Text style={styles.metricBig}>
              {snapshot.battery_power >= 0 ? '' : '−'}{fmt(Math.abs(snapshot.battery_power), 0)}
            </Text>
            <Text style={styles.metricUnit}>W</Text>
          </View>
          <Text style={styles.metricSub}>
            {snapshot.battery_current >= 0 ? 'DRAWING' : 'CHARGING'}  ·  PACK {fmt(snapshot.battery_temp, 1)} °C
          </Text>
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
});
