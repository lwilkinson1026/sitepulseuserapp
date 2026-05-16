// Push notification fan-out.
//
// Firestore trigger on units/{unitId}/events/{eventId}.
// On create: look up the unit's owner, read their pushTokens collection,
// and send an Expo Push notification per device. Failures are logged but
// not retried — the event log itself is the durable record.
//
// We use Expo's push service (not raw FCM/APNs) because the client app is
// Expo-managed and registers an ExponentPushToken; Expo handles delivery
// to both APNs and FCM without us touching either credential.

import { onDocumentCreated } from 'firebase-functions/v2/firestore';
import { logger } from 'firebase-functions';
import * as admin from 'firebase-admin';
import { Expo, ExpoPushMessage } from 'expo-server-sdk';

if (!admin.apps.length) {
  admin.initializeApp();
}

const db = admin.firestore();
const expo = new Expo();

// Map event kinds → user-facing notification copy. Kinds not listed here
// are silently skipped (logged only) — useful for internal-only events
// like `system.online` that we don't want to wake the user for.
const COPY: Record<string, { title: string; body: (payload: Record<string, unknown>) => string }> = {
  motion: {
    title: 'Motion detected',
    body: () => 'Sentry recorded a new clip. Tap to review.',
  },
  'soc.critical': {
    title: 'Battery critically low',
    body: (p) => `SoC at ${p.soc ?? '??'}% — engine charging blocked by quiet hours. Tap to override.`,
  },
  'engine.start': {
    title: 'Engine started',
    body: (p) => `Recharging — ${p.reason ?? 'scheduled run'}.`,
  },
  'engine.stop': {
    title: 'Engine stopped',
    body: (p) => `Recharge complete — ${p.reason ?? 'SoC target reached'}.`,
  },
  'theft.tripped': {
    title: 'Geofence tripped',
    body: () => 'The unit moved outside its geofence. Tap for live location.',
  },
};

export const onEventCreated = onDocumentCreated(
  'units/{unitId}/events/{eventId}',
  async (event) => {
    const snap = event.data;
    if (!snap) return;
    const data = snap.data() as { kind?: string; payload?: Record<string, unknown> };
    const unitId = event.params.unitId;
    const kind = data.kind ?? '';

    const copy = COPY[kind];
    if (!copy) {
      logger.info(`event/${kind} — no push copy registered, skipping`);
      return;
    }

    // Find the owner of this unit.
    const unitDoc = await db.doc(`units/${unitId}`).get();
    const ownerId = unitDoc.get('ownerId') as string | null;
    if (!ownerId) {
      logger.warn(`event/${kind} on unit ${unitId} — no owner, skipping`);
      return;
    }

    // Pull push tokens.
    const tokensSnap = await db.collection(`users/${ownerId}/pushTokens`).get();
    if (tokensSnap.empty) {
      logger.info(`event/${kind} for ${ownerId} — no push tokens registered`);
      return;
    }

    const messages: ExpoPushMessage[] = [];
    const invalidTokens: string[] = [];
    tokensSnap.forEach((doc) => {
      const token = doc.get('token') as string;
      if (!Expo.isExpoPushToken(token)) {
        invalidTokens.push(doc.id);
        return;
      }
      messages.push({
        to: token,
        sound: 'default',
        title: copy.title,
        body: copy.body(data.payload ?? {}),
        data: { unitId, eventId: event.params.eventId, kind },
      });
    });

    // Prune invalid tokens so we don't keep paying for them.
    await Promise.all(
      invalidTokens.map((id) =>
        db.doc(`users/${ownerId}/pushTokens/${id}`).delete().catch(() => {})
      ),
    );

    // Expo batches up to 100 messages per chunk.
    const chunks = expo.chunkPushNotifications(messages);
    for (const chunk of chunks) {
      try {
        const tickets = await expo.sendPushNotificationsAsync(chunk);
        logger.info(`event/${kind} sent ${tickets.length} push ticket(s)`);
      } catch (err) {
        logger.error(`event/${kind} push send failed`, err);
      }
    }
  },
);
