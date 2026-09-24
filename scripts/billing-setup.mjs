// Turn a unit into a Stripe-billed rental.
//
// Idempotent: finds-or-creates the Stripe product/price (by lookup_key) and a
// Customer Portal configuration with cancellation DISABLED (the rental
// agreement requires 30 days' written notice), then writes
// units/{UNIT_ID}/billing/plan. Re-run any time to change the first charge
// date or payment methods; it never touches an existing subscription.
//
// Usage (test mode first — use an sk_test_ key):
//   UNIT_ID=UNIT-002 FIRST_CHARGE=2026-09-26T09:00:00-04:00 \
//     node scripts/billing-setup.mjs
//
// It asks for the Stripe secret key with hidden input (so it stays out of
// shell history); STRIPE_SECRET_KEY in the environment skips the prompt.
//
// Optional:
//   PAYMENT_METHODS=card,us_bank_account   (default: card)
//   AMOUNT_CENTS=24900                     (default: 24900 = $249/mo)
//   FIRST_CHARGE=none                      (charge immediately at checkout)
//   BILLING_EMAIL=ap@customer.com          (receipts go here, not the app login)
//   SERVICE_ACCOUNT=./scripts/service-account.json
//
// Firestore auth: uses the service-account file if present, otherwise your
// own Google login via Application Default Credentials — run
// `gcloud auth application-default login` once first.

import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { initializeApp, cert } from 'firebase-admin/app';
import { getFirestore, Timestamp, FieldValue } from 'firebase-admin/firestore';

// stripe is a dependency of functions/, not the app — borrow it from there.
const require = createRequire(new URL('../functions/package.json', import.meta.url));
const Stripe = require('stripe');

const UNIT_ID = process.env.UNIT_ID;
const KEY = process.env.STRIPE_SECRET_KEY || (await promptHidden('Stripe secret key (sk_test_… or sk_live_…): '));
const SA_PATH = process.env.SERVICE_ACCOUNT ?? './scripts/service-account.json';
const AMOUNT_CENTS = Number(process.env.AMOUNT_CENTS ?? 24900);
const PAYMENT_METHODS = (process.env.PAYMENT_METHODS ?? 'card').split(',').map((s) => s.trim());
const FIRST_CHARGE = process.env.FIRST_CHARGE ?? null;
const BILLING_EMAIL = (process.env.BILLING_EMAIL ?? '').trim() || null;

if (!UNIT_ID) throw new Error('UNIT_ID is required (e.g. UNIT-002).');
if (!/^(sk|rk)_(test|live)_/.test(KEY)) throw new Error('That does not look like a Stripe secret key.');

const LOOKUP_KEY = `sitepulse_v1_rental_monthly_${AMOUNT_CENTS}`;
const PORTAL_TAG = 'sitepulse-rental-v1';
const stripe = new Stripe(KEY);
const mode = KEY.startsWith('sk_live_') ? 'LIVE' : 'TEST';
console.log(`Stripe mode: ${mode}`);

// ── price (+ product) ──────────────────────────────────────────────────────
let price = (await stripe.prices.list({ lookup_keys: [LOOKUP_KEY], limit: 1 })).data[0];
if (!price) {
  price = await stripe.prices.create({
    lookup_key: LOOKUP_KEY,
    currency: 'usd',
    unit_amount: AMOUNT_CENTS,
    recurring: { interval: 'month' },
    product_data: {
      name: 'SitePulse V1 Hybrid Power Station — Monthly Rental',
      statement_descriptor: 'SITEPULSE RENTAL',
    },
  });
  console.log(`Created price ${price.id}`);
} else {
  console.log(`Using price ${price.id}`);
}

// ── customer portal config: card + invoices, no self-serve cancel ──────────
const configs = await stripe.billingPortal.configurations.list({ limit: 100, active: true });
let portal = configs.data.find((c) => c.metadata?.tag === PORTAL_TAG);
if (!portal) {
  portal = await stripe.billingPortal.configurations.create({
    metadata: { tag: PORTAL_TAG },
    business_profile: { headline: 'SitePulse rental billing' },
    features: {
      payment_method_update: { enabled: true },
      invoice_history: { enabled: true },
      customer_update: { enabled: true, allowed_updates: ['email', 'address', 'name', 'tax_id'] },
      subscription_cancel: { enabled: false },
      subscription_update: { enabled: false },
    },
  });
  console.log(`Created portal configuration ${portal.id}`);
} else {
  console.log(`Using portal configuration ${portal.id}`);
}

// ── plan doc ───────────────────────────────────────────────────────────────
if (existsSync(SA_PATH)) {
  initializeApp({ credential: cert(JSON.parse(readFileSync(SA_PATH, 'utf-8'))) });
} else {
  initializeApp({ projectId: process.env.FIREBASE_PROJECT ?? 'sitepulse-userapp' });
}
const db = getFirestore();

const unit = await db.doc(`units/${UNIT_ID}`).get();
if (!unit.exists) throw new Error(`units/${UNIT_ID} does not exist.`);
console.log(`Unit ${UNIT_ID} owner uid: ${unit.get('ownerId') ?? '(none!)'}`);

let firstChargeAt = FieldValue.delete();
if (FIRST_CHARGE && FIRST_CHARGE !== 'none') {
  const d = new Date(FIRST_CHARGE);
  if (Number.isNaN(d.getTime())) throw new Error(`Bad FIRST_CHARGE: ${FIRST_CHARGE}`);
  firstChargeAt = Timestamp.fromDate(d);
  console.log(`First charge: ${d.toString()}`);
}

await db.doc(`units/${UNIT_ID}/billing/plan`).set(
  {
    priceId: price.id,
    portalConfigurationId: portal.id,
    label: 'SitePulse V1 rental',
    amountCents: AMOUNT_CENTS,
    currency: 'usd',
    interval: 'month',
    paymentMethodTypes: PAYMENT_METHODS,
    billingEmail: BILLING_EMAIL ?? FieldValue.delete(),
    firstChargeAt,
    stripeMode: mode,
    updatedAt: FieldValue.serverTimestamp(),
  },
  { merge: true },
);

console.log(`Wrote units/${UNIT_ID}/billing/plan (${mode}).`);
process.exit(0);

// Read a line from the TTY without echoing it.
function promptHidden(question) {
  return new Promise((resolve) => {
    const { stdin, stdout } = process;
    stdout.write(question);
    stdin.setRawMode(true);
    stdin.resume();
    stdin.setEncoding('utf8');
    let value = '';
    const onData = (ch) => {
      if (ch === '\r' || ch === '\n' || ch === '\u0004') {
        stdin.setRawMode(false);
        stdin.pause();
        stdin.off('data', onData);
        stdout.write('\n');
        resolve(value.trim());
      } else if (ch === '\u0003') {
        stdout.write('\n');
        process.exit(130);
      } else if (ch === '\u007f') {
        value = value.slice(0, -1);
      } else {
        value += ch;
      }
    };
    stdin.on('data', onData);
  });
}
