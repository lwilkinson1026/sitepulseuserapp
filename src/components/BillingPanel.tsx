// Rental billing panel (Activity tab) + a Dashboard strip for when action is
// needed. Both render nothing for units without a billing plan.
//
// All state comes from Firestore docs the Stripe webhook maintains; buttons
// only ever hand off to Stripe-hosted pages (Checkout / Customer Portal).

import React, { useState } from 'react';
import { Linking, Platform, Pressable, StyleSheet, Text, View } from 'react-native';
import type { Timestamp } from 'firebase/firestore';
import { Eyebrow } from './Eyebrow';
import { PrimaryCTA } from './PrimaryCTA';
import { SecondaryCTA } from './SecondaryCTA';
import { formatMoney, useBilling } from '../hooks/useBilling';
import { openBillingPortal, startRentalCheckout } from '../firebase/billing';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../theme';

function fmtDate(ts: Timestamp | null | undefined): string {
  if (!ts) return '—';
  return ts
    .toDate()
    .toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })
    .toUpperCase();
}

// Stripe sends the browser back with ?billing=success before the webhook has
// necessarily landed; show "confirming" instead of flashing "set up payment".
function justReturnedFromCheckout(): boolean {
  if (Platform.OS !== 'web' || typeof window === 'undefined') return false;
  return new URLSearchParams(window.location.search).get('billing') === 'success';
}

function useBillingAction(unitId: string | null) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (fn: (id: string) => Promise<void>) => {
    if (!unitId || busy) return;
    setBusy(true);
    setError(null);
    try {
      await fn(unitId);
      // On web the page navigates away; leave `busy` set so the button can't
      // be double-tapped during the redirect.
      if (Platform.OS !== 'web') setBusy(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };
  return { busy, error, run };
}

export function BillingPanel({ unitId }: { unitId: string | null }) {
  const { phase, plan, subscription } = useBilling(unitId);
  const { busy, error, run } = useBillingAction(unitId);
  if (phase === 'none' || !plan) return null;

  const price = `${formatMoney(plan.amountCents, plan.currency)} / ${plan.interval.toUpperCase()}`;
  const pm = subscription?.paymentMethod;
  const pmLabel = pm?.last4 ? `${(pm.brand ?? pm.type).toUpperCase()} •••• ${pm.last4}` : '—';
  const confirming = phase === 'needs_setup' && justReturnedFromCheckout();

  const statusLabel =
    confirming ? 'Confirming' :
    phase === 'active' ? (subscription?.cancelAt ? 'Ending' : 'Active') :
    phase === 'past_due' ? 'Payment failed' :
    phase === 'ended' ? 'Ended' : 'Payment needed';
  const statusColor =
    phase === 'active' ? colors.live :
    phase === 'past_due' ? colors.danger :
    confirming ? colors.textBody : colors.warning;

  return (
    <View style={styles.container}>
      <Eyebrow parts={['Rental', plan.label]} />

      <View style={[styles.status, { borderColor: statusColor }]}>
        <View style={[styles.dot, { backgroundColor: statusColor }]} />
        <Text style={[styles.statusText, { color: statusColor }]}>{statusLabel.toUpperCase()}</Text>
        <Text style={styles.statusDetail}>{price}</Text>
      </View>

      <View style={styles.rows}>
        {phase === 'active' || phase === 'past_due' ? (
          <>
            <Row
              label={subscription?.cancelAt ? 'Ends' : 'Next charge'}
              value={fmtDate(subscription?.cancelAt ?? subscription?.currentPeriodEnd)}
            />
            <Row label="Payment method" value={pmLabel} />
          </>
        ) : phase === 'needs_setup' && plan.firstChargeAt ? (
          <Row label="First charge" value={fmtDate(plan.firstChargeAt)} />
        ) : null}
      </View>

      {confirming ? (
        <Text style={styles.note}>
          PAYMENT RECEIVED BY STRIPE — THIS UPDATES AUTOMATICALLY IN A FEW SECONDS.
        </Text>
      ) : phase === 'needs_setup' || phase === 'ended' ? (
        <>
          <Text style={styles.note}>
            {plan.firstChargeAt && plan.firstChargeAt.toMillis() > Date.now()
              ? `ADD A CARD NOW. NOTHING IS CHARGED UNTIL ${fmtDate(plan.firstChargeAt)}, THEN MONTHLY ON THAT DAY.`
              : 'ADD A CARD TO START THE MONTHLY RENTAL. THE FIRST MONTH IS CHARGED TODAY.'}
          </Text>
          <PrimaryCTA
            label={busy ? 'Opening Stripe…' : 'Set up payment'}
            onPress={() => run(startRentalCheckout)}
            disabled={busy}
          />
        </>
      ) : (
        <>
          {phase === 'past_due' && subscription?.latestInvoice?.hostedInvoiceUrl ? (
            <PrimaryCTA
              label="Pay invoice"
              onPress={() => Linking.openURL(subscription.latestInvoice!.hostedInvoiceUrl!)}
            />
          ) : null}
          <SecondaryCTA
            label={busy ? 'Opening Stripe…' : phase === 'past_due' ? 'Update card' : 'Manage billing'}
            onPress={() => run(openBillingPortal)}
            disabled={busy}
          />
          <Text style={styles.note}>
            RECEIPTS AND INVOICES ARE IN MANAGE BILLING. TO END THE RENTAL, GIVE 30 DAYS' NOTICE
            TO SITEPULSE.
          </Text>
        </>
      )}

      {error ? <Text style={styles.error}>{error.toUpperCase()}</Text> : null}
    </View>
  );
}

/** Dashboard strip — only when the rental needs the owner to do something. */
export function BillingAlertBanner({ unitId }: { unitId: string | null }) {
  const { phase } = useBilling(unitId);
  const { busy, run } = useBillingAction(unitId);
  if (phase !== 'needs_setup' && phase !== 'past_due') return null;
  if (justReturnedFromCheckout()) return null;

  const pastDue = phase === 'past_due';
  const color = pastDue ? colors.danger : colors.warning;
  return (
    <Pressable
      onPress={() => run(pastDue ? openBillingPortal : startRentalCheckout)}
      disabled={busy}
      style={({ pressed }) => [styles.banner, { borderColor: color }, pressed ? { opacity: 0.7 } : null]}
      accessibilityRole="button"
    >
      <View style={[styles.dot, { backgroundColor: color }]} />
      <Text style={[styles.statusText, { color }]}>
        {pastDue ? 'RENTAL PAYMENT FAILED' : 'RENTAL PAYMENT NEEDED'}
      </Text>
      <Text style={styles.statusDetail}>{busy ? 'OPENING…' : pastDue ? 'UPDATE CARD →' : 'SET UP →'}</Text>
    </Pressable>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.row}>
      <Text style={styles.rowLabel}>{label.toUpperCase()}</Text>
      <Text style={styles.rowValue}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    gap: spacing.md,
    paddingBottom: spacing.lg,
  },
  status: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.xs,
    borderWidth: hairline,
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.sm,
  },
  banner: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.xs,
    borderWidth: hairline,
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.sm,
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: 4,
  },
  statusText: {
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  statusDetail: {
    flex: 1,
    textAlign: 'right',
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  rows: {
    gap: spacing.xs,
  },
  row: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    borderBottomWidth: hairline,
    borderBottomColor: colors.borderHairline,
    paddingBottom: spacing.xs,
  },
  rowLabel: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  rowValue: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  note: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
    lineHeight: 16,
  },
  error: {
    color: colors.danger,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
});
