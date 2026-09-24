// Rental billing via Stripe.
//
// Billing is per UNIT, not per user: a rental is for a specific generator,
// and whoever owns that unit in Firestore is the one who pays for it.
//
// Firestore layout (client reads, never writes — see firestore.rules):
//
//   units/{unitId}/billing/plan          written by scripts/billing-setup.mjs
//     { priceId, portalConfigurationId, firstChargeAt?, paymentMethodTypes?,
//       label, amountCents, currency, interval }
//
//   units/{unitId}/billing/subscription  written ONLY by stripeWebhook
//     { customerId, subscriptionId, status, currentPeriodEnd, cancelAt,
//       cancelAtPeriodEnd, paymentMethod, latestInvoice, updatedAt }
//
// Flow: the app calls createRentalCheckout → opens Stripe Checkout in the
// browser → Stripe calls stripeWebhook → the subscription doc updates → the
// app's snapshot listener re-renders. The app never sees card data and never
// decides for itself whether a subscription is active.
//
// Secrets (set with `firebase functions:secrets:set …`, never in the repo):
//   STRIPE_SECRET_KEY      sk_test_… / sk_live_…
//   STRIPE_WEBHOOK_SECRET  whsec_… from the webhook endpoint in the dashboard

import { onCall, onRequest, HttpsError } from 'firebase-functions/v2/https';
import { defineSecret } from 'firebase-functions/params';
import { logger } from 'firebase-functions';
import * as admin from 'firebase-admin';
import Stripe from 'stripe';

if (!admin.apps.length) {
  admin.initializeApp();
}

const db = admin.firestore();

const STRIPE_SECRET_KEY = defineSecret('STRIPE_SECRET_KEY');
const STRIPE_WEBHOOK_SECRET = defineSecret('STRIPE_WEBHOOK_SECRET');

// Created lazily: secrets are only readable inside a handler invocation.
let stripeClient: Stripe | null = null;
function stripe(): Stripe {
  if (!stripeClient) stripeClient = new Stripe(STRIPE_SECRET_KEY.value());
  return stripeClient;
}

// Statuses that mean "this unit already has a live rental" — starting a
// second Checkout would double-bill.
const LIVE_STATUSES = new Set(['active', 'trialing', 'past_due', 'unpaid']);

// Stripe rejects a billing_cycle_anchor that isn't comfortably in the future,
// and an anchor minutes away is pointless anyway. Inside this window we just
// bill immediately.
const MIN_ANCHOR_LEAD_MS = 60 * 60 * 1000;

type BillingPlan = {
  priceId: string;
  portalConfigurationId?: string;
  firstChargeAt?: admin.firestore.Timestamp;
  paymentMethodTypes?: string[];
  // Where Stripe sends receipts / failed-payment notices. Set when the app
  // login is shared (e.g. SitePulse's account) but the payer is the customer.
  billingEmail?: string;
};

async function requireOwner(uid: string | undefined, unitId: unknown): Promise<string> {
  if (!uid) throw new HttpsError('unauthenticated', 'Sign in first.');
  if (typeof unitId !== 'string' || !unitId) {
    throw new HttpsError('invalid-argument', 'unitId is required.');
  }
  const unit = await db.doc(`units/${unitId}`).get();
  if (!unit.exists || unit.get('ownerId') !== uid) {
    // Same error for "missing" and "not yours" so unit ids can't be probed.
    throw new HttpsError('permission-denied', 'You do not own this unit.');
  }
  return unitId;
}

// Return URLs come from the client, so only allow our own origins — otherwise
// Checkout becomes an open redirect wearing our brand.
function safeReturnUrl(raw: unknown): string {
  const fallback = 'https://sitepulse.space/';
  if (typeof raw !== 'string') return fallback;
  try {
    const u = new URL(raw);
    const okHost =
      u.hostname === 'localhost' ||
      u.hostname === 'sitepulse.space' ||
      u.hostname.endsWith('.sitepulse.space') ||
      u.hostname.endsWith('.vercel.app');
    return okHost && (u.protocol === 'https:' || u.hostname === 'localhost') ? raw : fallback;
  } catch {
    return fallback;
  }
}

/**
 * Start (or resume) the rental for a unit. Returns a Stripe Checkout URL.
 *
 * If the plan has a future `firstChargeAt`, the subscription is anchored to
 * it with no proration: the card is saved today, $0 is charged today, and the
 * first full month is charged on firstChargeAt — then the same day monthly.
 * That matches "payable in advance, due the day after the trial ends".
 */
export const createRentalCheckout = onCall(
  { secrets: [STRIPE_SECRET_KEY], invoker: 'public' },
  async (request) => {
    const unitId = await requireOwner(request.auth?.uid, request.data?.unitId);
    const returnUrl = safeReturnUrl(request.data?.returnUrl);

    const planSnap = await db.doc(`units/${unitId}/billing/plan`).get();
    const plan = planSnap.data() as BillingPlan | undefined;
    if (!plan?.priceId) {
      throw new HttpsError('failed-precondition', 'No rental plan is set up for this unit.');
    }

    const subRef = db.doc(`units/${unitId}/billing/subscription`);
    const existing = (await subRef.get()).data();
    if (existing?.status && LIVE_STATUSES.has(existing.status)) {
      throw new HttpsError('already-exists', 'This unit already has an active rental.');
    }

    // One Stripe customer per unit, reused across re-subscribes so invoice
    // history stays in one place.
    let customerId: string | undefined = existing?.customerId;
    if (!customerId) {
      const customer = await stripe().customers.create({
        email: plan.billingEmail ?? request.auth?.token.email,
        metadata: { unitId, ownerUid: request.auth!.uid },
      });
      customerId = customer.id;
      await subRef.set({ customerId }, { merge: true });
    }

    const firstChargeMs = plan.firstChargeAt?.toMillis();
    const anchor =
      firstChargeMs && firstChargeMs - Date.now() > MIN_ANCHOR_LEAD_MS
        ? Math.floor(firstChargeMs / 1000)
        : undefined;

    const session = await stripe().checkout.sessions.create({
      mode: 'subscription',
      customer: customerId,
      line_items: [{ price: plan.priceId, quantity: 1 }],
      // Card-only until a customer asks for ACH; add 'us_bank_account' to the
      // plan doc to offer it — no redeploy needed.
      payment_method_types: (plan.paymentMethodTypes ?? ['card']) as
        Stripe.Checkout.SessionCreateParams.PaymentMethodType[],
      client_reference_id: unitId,
      subscription_data: {
        metadata: { unitId },
        description: `SitePulse rental — ${unitId}`,
        ...(anchor ? { billing_cycle_anchor: anchor, proration_behavior: 'none' as const } : {}),
      },
      metadata: { unitId },
      success_url: withParam(returnUrl, 'billing', 'success'),
      cancel_url: withParam(returnUrl, 'billing', 'canceled'),
    });

    if (!session.url) throw new HttpsError('internal', 'Stripe did not return a Checkout URL.');
    return { url: session.url };
  },
);

/**
 * Stripe Customer Portal: update card, download invoices/receipts.
 * Cancellation is disabled in the portal configuration on purpose — the
 * rental agreement requires 30 days' written notice, so cancels go through
 * SitePulse, not a button.
 */
export const createBillingPortal = onCall(
  { secrets: [STRIPE_SECRET_KEY], invoker: 'public' },
  async (request) => {
    const unitId = await requireOwner(request.auth?.uid, request.data?.unitId);
    const returnUrl = safeReturnUrl(request.data?.returnUrl);

    const [planSnap, subSnap] = await Promise.all([
      db.doc(`units/${unitId}/billing/plan`).get(),
      db.doc(`units/${unitId}/billing/subscription`).get(),
    ]);
    const customerId = subSnap.get('customerId') as string | undefined;
    if (!customerId) {
      throw new HttpsError('failed-precondition', 'No billing account yet — set up payment first.');
    }
    const configuration = planSnap.get('portalConfigurationId') as string | undefined;

    const session = await stripe().billingPortal.sessions.create({
      customer: customerId,
      return_url: returnUrl,
      ...(configuration ? { configuration } : {}),
    });
    return { url: session.url };
  },
);

/**
 * Stripe → Firestore. Rather than trusting each event's payload (events can
 * arrive out of order), every relevant event just triggers a fresh read of the
 * subscription and overwrites the snapshot doc. Replays are harmless.
 */
export const stripeWebhook = onRequest(
  { secrets: [STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET], invoker: 'public' },
  async (req, res) => {
    let event: Stripe.Event;
    try {
      event = stripe().webhooks.constructEvent(
        req.rawBody,
        req.headers['stripe-signature'] as string,
        STRIPE_WEBHOOK_SECRET.value(),
      );
    } catch (err) {
      logger.warn('[billing] webhook signature check failed', err);
      res.status(400).send('Bad signature');
      return;
    }

    try {
      const subscriptionId = subscriptionIdFor(event);
      if (subscriptionId) {
        await syncSubscription(subscriptionId);
      } else {
        logger.debug(`[billing] ignoring ${event.type}`);
      }
      res.status(200).send('ok');
    } catch (err) {
      // 500 makes Stripe retry with backoff, which is what we want for a
      // transient Firestore/Stripe failure.
      logger.error(`[billing] failed handling ${event.type} ${event.id}`, err);
      res.status(500).send('error');
    }
  },
);

function subscriptionIdFor(event: Stripe.Event): string | null {
  switch (event.type) {
    case 'checkout.session.completed': {
      const s = event.data.object;
      return typeof s.subscription === 'string' ? s.subscription : s.subscription?.id ?? null;
    }
    case 'customer.subscription.created':
    case 'customer.subscription.updated':
    case 'customer.subscription.deleted':
      return event.data.object.id;
    case 'invoice.paid':
    case 'invoice.payment_failed': {
      const sub = event.data.object.parent?.subscription_details?.subscription;
      return typeof sub === 'string' ? sub : sub?.id ?? null;
    }
    default:
      return null;
  }
}

async function syncSubscription(subscriptionId: string): Promise<void> {
  const sub = await stripe().subscriptions.retrieve(subscriptionId, {
    expand: ['default_payment_method', 'latest_invoice'],
  });
  const unitId = sub.metadata?.unitId;
  if (!unitId) {
    // A subscription created by hand in the dashboard without metadata.
    // Log loudly — it needs `unitId` metadata added to show up in the app.
    logger.warn(`[billing] subscription ${sub.id} has no unitId metadata; skipping`);
    return;
  }

  const pm = sub.default_payment_method;
  const paymentMethod =
    pm && typeof pm !== 'string'
      ? pm.card
        ? { type: 'card', brand: pm.card.brand, last4: pm.card.last4 }
        : pm.us_bank_account
        ? { type: 'us_bank_account', brand: pm.us_bank_account.bank_name ?? 'Bank', last4: pm.us_bank_account.last4 }
        : { type: pm.type, brand: null, last4: null }
      : null;

  const inv = sub.latest_invoice;
  const latestInvoice =
    inv && typeof inv !== 'string'
      ? {
          id: inv.id,
          status: inv.status,
          amountDue: inv.amount_due,
          amountPaid: inv.amount_paid,
          currency: inv.currency,
          hostedInvoiceUrl: inv.hosted_invoice_url ?? null,
          created: admin.firestore.Timestamp.fromMillis(inv.created * 1000),
        }
      : null;

  // current_period_end lives on the item since API 2025-03-31.
  const periodEnd = sub.items.data[0]?.current_period_end;
  const customerId = typeof sub.customer === 'string' ? sub.customer : sub.customer.id;

  await db.doc(`units/${unitId}/billing/subscription`).set(
    {
      customerId,
      subscriptionId: sub.id,
      status: sub.status,
      currentPeriodEnd: periodEnd ? admin.firestore.Timestamp.fromMillis(periodEnd * 1000) : null,
      cancelAt: sub.cancel_at ? admin.firestore.Timestamp.fromMillis(sub.cancel_at * 1000) : null,
      cancelAtPeriodEnd: sub.cancel_at_period_end,
      paymentMethod,
      latestInvoice,
      updatedAt: admin.firestore.FieldValue.serverTimestamp(),
    },
    { merge: true },
  );
  logger.info(`[billing] ${unitId}: ${sub.id} → ${sub.status}`);
}

function withParam(url: string, key: string, value: string): string {
  const u = new URL(url);
  u.searchParams.set(key, value);
  return u.toString();
}
