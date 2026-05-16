import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { Eyebrow, FigCaption, Screen } from '../../components';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../../theme';

const DAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];

export function ScheduleScreen() {
  return (
    <Screen>
      <View style={styles.header}>
        <Eyebrow parts={['03 / Schedule', 'Quiet hours']} />
        <Text style={styles.headline}>Engine{'\n'}may run:</Text>
      </View>

      <View style={styles.grid}>
        {DAYS.map((d) => (
          <View key={d} style={styles.dayRow}>
            <Text style={styles.dayLabel}>{d}</Text>
            <View style={styles.windowBar}>
              <View style={styles.window} />
            </View>
            <Text style={styles.windowLabel}>08:00 — 18:00</Text>
          </View>
        ))}
      </View>

      <View style={styles.footer}>
        <FigCaption number={3} label="Schedule" detail="MST · UNIT-001" />
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  header: { paddingTop: spacing.md, paddingBottom: spacing.lg },
  headline: {
    marginTop: spacing.md,
    color: colors.textDisplay,
    fontFamily: fonts.display,
    fontSize: typeScale.displayMD,
    lineHeight: typeScale.displayMD,
    letterSpacing: tracking.displayTight,
  },
  grid: {
    flex: 1,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
  },
  dayRow: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingVertical: spacing.sm,
    borderBottomWidth: hairline,
    borderBottomColor: colors.borderHairline,
    gap: spacing.sm,
  },
  dayLabel: {
    width: 40,
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  windowBar: {
    flex: 1,
    height: 4,
    backgroundColor: colors.borderHairline,
  },
  window: {
    width: '42%',
    marginLeft: '33%',
    height: '100%',
    backgroundColor: colors.textDisplay,
  },
  windowLabel: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  footer: { paddingVertical: spacing.md },
});
