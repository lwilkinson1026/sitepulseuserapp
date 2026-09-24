// Client side of rental billing. Both calls return a Stripe-hosted URL which
// we open in the browser — card details never touch the app.

import { Linking, Platform } from 'react-native';
import { getFunctions, httpsCallable } from 'firebase/functions';
import { getFirebase } from './config';

type UrlResult = { url: string };

// Where Stripe sends the user back to. On web that's the page they came from;
// native has no https origin of its own, so the function falls back to a safe
// default.
function returnUrl(): string | undefined {
  if (Platform.OS === 'web' && typeof window !== 'undefined') {
    return window.location.origin + window.location.pathname;
  }
  return undefined;
}

async function openHosted(name: 'createRentalCheckout' | 'createBillingPortal', unitId: string) {
  const { app } = getFirebase();
  const call = httpsCallable<{ unitId: string; returnUrl?: string }, UrlResult>(
    getFunctions(app),
    name,
  );
  const { data } = await call({ unitId, returnUrl: returnUrl() });
  if (Platform.OS === 'web' && typeof window !== 'undefined') {
    // Same-tab navigation: a window.open() after an await is popup-blocked.
    window.location.assign(data.url);
  } else {
    await Linking.openURL(data.url);
  }
}

export const startRentalCheckout = (unitId: string) => openHosted('createRentalCheckout', unitId);
export const openBillingPortal = (unitId: string) => openHosted('createBillingPortal', unitId);
