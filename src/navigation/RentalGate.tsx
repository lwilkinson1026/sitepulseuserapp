// First-use gate for rental units.
//
// When a unit has a billing plan (units/{id}/billing/plan) but the owner has
// never completed Stripe Checkout, they land here instead of the tabs. Adding
// a card is then part of setting the unit up, not an email we chase later.
// Once the webhook marks the subscription active the gate lifts on its own.
//
// Deliberately narrow — this is onboarding, not a lockout:
//  - only the 'needs_setup' phase (no rental ever started). A failed charge
//    ('past_due') or an ended rental never hides the unit: the rental
//    agreement gives no right to disable a unit for non-payment.
//  - only the unit's owner. Fleet admins viewing a customer's unit go straight
//    through (they can't pay for it anyway).
//  - units without a plan (our own bench units) are unaffected.

import React from 'react';
import { ActivityIndicator, ScrollView, StyleSheet, Text, View } from 'react-native';
import { BillingPanel, Eyebrow, Screen, SecondaryCTA, UnitSwitcher } from '../components';
import { useActiveUnit } from '../hooks/ActiveUnitContext';
import { useAuth } from '../hooks/AuthContext';
import { useBilling } from '../hooks/useBilling';
import { colors, fonts, spacing, tracking, typeScale } from '../theme';

export function RentalGate({ children }: { children: React.ReactNode }) {
  const { unitId, isOwner } = useActiveUnit();
  const { loading, phase } = useBilling(isOwner ? unitId : null);

  if (!isOwner || !unitId) return <>{children}</>;
  // Hold the tabs back until billing state is known, so a rental customer
  // doesn't see the dashboard flash before the setup screen.
  if (loading) {
    return (
      <View style={styles.loading}>
        <ActivityIndicator color={colors.textMuted} />
      </View>
    );
  }
  if (phase !== 'needs_setup') return <>{children}</>;
  return <RentalSetupScreen unitId={unitId} />;
}

function RentalSetupScreen({ unitId }: { unitId: string }) {
  const { signOutNow, user } = useAuth();
  return (
    <Screen edges={['top', 'left', 'right', 'bottom']}>
      <ScrollView contentContainerStyle={styles.scroll} showsVerticalScrollIndicator={false}>
        <UnitSwitcher />
        <Eyebrow parts={['Setup', unitId]} />
        <Text style={styles.title}>Activate your rental</Text>
        <Text style={styles.body}>
          Add a payment card to finish setting up {unitId}. Your dashboard, outlets, schedule and
          camera open as soon as the card is saved. Card details are entered on Stripe's secure
          page and never stored in this app.
        </Text>

        <BillingPanel unitId={unitId} />

        <View style={styles.account}>
          <Text style={styles.accountLabel}>SIGNED IN AS</Text>
          <Text style={styles.accountValue}>{user?.email ?? '—'}</Text>
          <View style={{ marginTop: spacing.md }}>
            <SecondaryCTA label="Sign out" onPress={signOutNow} />
          </View>
        </View>
      </ScrollView>
    </Screen>
  );
}

const styles = StyleSheet.create({
  loading: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.background,
  },
  scroll: {
    gap: spacing.lg,
    paddingVertical: spacing.lg,
  },
  title: {
    color: colors.textDisplay,
    fontFamily: fonts.display,
    fontSize: typeScale.displayMD,
    letterSpacing: tracking.displayNormal,
  },
  body: {
    color: colors.textBody,
    fontSize: 15,
    lineHeight: 22,
  },
  account: {
    marginTop: spacing.lg,
  },
  accountLabel: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  accountValue: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
    marginTop: spacing.xs,
  },
});
