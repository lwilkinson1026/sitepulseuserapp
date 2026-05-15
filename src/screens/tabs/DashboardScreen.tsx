import React from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';
import { CornerBrackets, Eyebrow, FigCaption, Screen } from '../../components';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../../theme';

// Phase-0 dashboard shell with mocked values. Real telemetry binding lands
// in Phase 1 once the Firestore listener on units/{unitId}/current is wired.

const MOCK = {
  unit: 'UNIT-001',
  site: 'Bozeman Plant',
  soc: 87,
  load: 1240,
  voltage: 229.8,
  freq: 50.02,
  loadPct: 42,
  mode: 'BATTERY ONLY',
};

export function DashboardScreen() {
  return (
    <Screen>
      <ScrollView
        contentContainerStyle={styles.scroll}
        showsVerticalScrollIndicator={false}
      >
        <Eyebrow parts={[MOCK.unit, MOCK.site, 'LIVE']} live />

        <CornerBrackets style={styles.hero}>
          <View style={styles.heroInner}>
            <Text style={styles.heroNumeric}>{MOCK.soc}</Text>
            <Text style={styles.heroUnit}>%</Text>
          </View>
          <Text style={styles.heroSub}>STATE OF CHARGE  ·  48 V LiFePO4  ·  CYCLE 2,418</Text>
        </CornerBrackets>

        <View style={styles.modeRow}>
          <Text style={styles.modeBadge}>{MOCK.mode}</Text>
          <Text style={styles.modeStamp}>UPDATED 2 s AGO</Text>
        </View>

        <View style={styles.metricGroup}>
          <Eyebrow parts={['Inverter', '230 V AC', `${MOCK.loadPct}% LOAD`]} />
          <View style={styles.metricRow}>
            <Text style={styles.metricBig}>{MOCK.load}</Text>
            <Text style={styles.metricUnit}>W</Text>
          </View>
          <View style={styles.loadBar}>
            <View style={[styles.loadFill, { width: `${MOCK.loadPct}%` }]} />
          </View>
          <Text style={styles.metricSub}>
            {MOCK.voltage.toFixed(1)} V  ·  {MOCK.freq.toFixed(2)} Hz
          </Text>
        </View>

        <FigCaption number={1} label="Dashboard" detail={MOCK.unit} />
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
  hero: {
    paddingVertical: spacing.xl,
    paddingHorizontal: spacing.md,
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
    fontSize: typeScale.displayXL * 2.1,         // hero numeric fills the canvas
    lineHeight: typeScale.displayXL * 2.0,
    letterSpacing: tracking.displayTight * 2,
  },
  heroUnit: {
    color: colors.textMuted,
    fontFamily: fonts.display,
    fontSize: typeScale.displayMD,
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
  loadBar: {
    height: 2,
    backgroundColor: colors.borderHairline,
    overflow: 'hidden',
  },
  loadFill: {
    height: '100%',
    backgroundColor: colors.textDisplay,
  },
});
