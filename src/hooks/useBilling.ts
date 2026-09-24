// Rental billing state for a unit, reduced to what the UI needs to decide.
//
// `plan === null` means the unit isn't a rental (e.g. our own bench units) and
// every billing surface renders nothing.

import { useUnitDoc } from './useUnitDoc';
import type { BillingPlanDoc, BillingSubscriptionDoc } from '../firebase/types';

export type BillingPhase =
  | 'none'          // not a rental
  | 'needs_setup'   // plan exists, no live subscription — ask for a card
  | 'active'        // paid up (or card saved, first charge scheduled)
  | 'past_due'      // a charge failed; Stripe is retrying
  | 'ended';        // canceled after having been live

export function useBilling(unitId: string | null) {
  const plan = useUnitDoc<BillingPlanDoc>(unitId, 'billing', 'plan');
  const sub = useUnitDoc<BillingSubscriptionDoc>(unitId, 'billing', 'subscription');

  const status = sub.data?.status;
  let phase: BillingPhase;
  if (!plan.data) phase = 'none';
  else if (status === 'active' || status === 'trialing') phase = 'active';
  else if (status === 'past_due' || status === 'unpaid') phase = 'past_due';
  else if (status === 'canceled' && sub.data?.subscriptionId) phase = 'ended';
  else phase = 'needs_setup';

  return {
    loading: plan.loading || sub.loading,
    phase,
    plan: plan.data,
    subscription: sub.data,
  };
}

export function formatMoney(cents: number, currency = 'usd'): string {
  const amount = cents / 100;
  return `${currency.toLowerCase() === 'usd' ? '$' : ''}${
    Number.isInteger(amount) ? amount.toFixed(0) : amount.toFixed(2)
  }${currency.toLowerCase() === 'usd' ? '' : ` ${currency.toUpperCase()}`}`;
}
