import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { Eyebrow, FigCaption, Screen, SecondaryCTA } from '../../components';
import { useAuth } from '../../hooks/AuthContext';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../../theme';

// Reverse-chronological event log placeholder. Reads units/{unitId}/events
// in Phase 1. For scaffold purposes we render mock events and expose the
// sign-out CTA here so the auth state machine has a way back out.

const EVENTS = [
  { ts: '14:42:08', kind: 'OUTLET TOGGLED', body: 'CH 4 · WELDER · OFF' },
  { ts: '14:39:51', kind: 'SCHEDULE OVERRIDE', body: 'PAUSE · 2 H' },
  { ts: '11:02:14', kind: 'LOW SOC ALERT', body: 'SOC 19% · CHARGE STARTED' },
  { ts: '08:00:00', kind: 'QUIET HOURS LIFTED', body: 'ENGINE NOW PERMITTED' },
  { ts: 'YESTERDAY', kind: 'OWNERSHIP CONFIRMED', body: 'UNIT-001 BOUND' },
];

export function ActivityScreen() {
  const { signOutNow, user } = useAuth();

  return (
    <Screen>
      <View style={styles.header}>
        <Eyebrow parts={['04 / Activity', 'Event log']} />
      </View>

      <View style={styles.list}>
        {EVENTS.map((e, idx) => (
          <View key={idx} style={styles.row}>
            <Text style={styles.ts}>{e.ts}</Text>
            <View style={styles.entry}>
              <Text style={styles.kind}>{e.kind}</Text>
              <Text style={styles.body}>{e.body}</Text>
            </View>
          </View>
        ))}
      </View>

      <View style={styles.account}>
        <Text style={styles.accountLabel}>SIGNED IN AS</Text>
        <Text style={styles.accountValue}>{user?.email ?? '—'}</Text>
        <View style={{ marginTop: spacing.md }}>
          <SecondaryCTA label="Sign out" onPress={signOutNow} />
        </View>
      </View>

      <View style={styles.footer}>
        <FigCaption number={4} label="Activity" detail="UNIT-001" />
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  header: { paddingTop: spacing.md, paddingBottom: spacing.lg },
  list: {
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
  },
  row: {
    flexDirection: 'row',
    gap: spacing.md,
    paddingVertical: spacing.sm,
    borderBottomWidth: hairline,
    borderBottomColor: colors.borderHairline,
  },
  ts: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
    width: 86,
  },
  entry: { flex: 1 },
  kind: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  body: {
    marginTop: 2,
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  account: {
    marginTop: spacing.xl,
    paddingTop: spacing.lg,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
  },
  accountLabel: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  accountValue: {
    marginTop: spacing.xxs,
    color: colors.textDisplay,
    fontFamily: fonts.bodyMedium,
    fontSize: typeScale.bodyLG,
  },
  footer: { paddingVertical: spacing.md },
});
