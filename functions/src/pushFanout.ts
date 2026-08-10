// Notification fan-out.
//
// Firestore trigger on units/{unitId}/events/{eventId}.
// On create: look up the unit's owner, read their pushTokens collection,
// and send an Expo Push notification per device. Independently, send a
// Telegram message to any chat IDs configured on the unit. Failures are
// logged but not retried — the event log itself is the durable record.
//
// We use Expo's push service (not raw FCM/APNs) because the client app is
// Expo-managed and registers an ExponentPushToken; Expo handles delivery
// to both APNs and FCM without us touching either credential.
//
// Telegram: the bot token lives as a Function secret (TELEGRAM_BOT_TOKEN),
// never in the repo or on the field device. Recipient chat IDs live in
// units/{unitId}/config/notifications.telegramChatIds (an array of strings)
// so new recipients can be added without a redeploy.

import { onDocumentCreated } from 'firebase-functions/v2/firestore';
import { defineSecret } from 'firebase-functions/params';
import { logger } from 'firebase-functions';
import * as admin from 'firebase-admin';
import { Expo, ExpoPushMessage } from 'expo-server-sdk';

if (!admin.apps.length) {
  admin.initializeApp();
}

const db = admin.firestore();
const expo = new Expo();

const TELEGRAM_BOT_TOKEN = defineSecret('TELEGRAM_BOT_TOKEN');

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
    body: (p) => `Engine is now running — ${p.reason ?? p.state ?? 'started'}.`,
  },
  'engine.stop': {
    title: 'Engine stopped',
    body: (p) => `Engine has shut down — ${p.reason ?? p.state ?? 'stopped'}.`,
  },
  'theft.tripped': {
    title: 'Geofence tripped',
    body: () => 'The unit moved outside its geofence. Tap for live location.',
  },
  'fuel.low': {
    title: 'Low fuel',
    body: (p) =>
      typeof p.level === 'number'
        ? `~${(p.level as number).toFixed(1)} gal left. Plan a refuel soon.`
        : 'Fuel is running low. Plan a refuel soon.',
  },
  'fuel.empty': {
    title: 'Out of fuel',
    body: () => 'The tank is empty — the generator can no longer run. Refuel to resume.',
  },
  // Raspberry Pi host temperature. Emitted by pi/pi_health.py only on a
  // SUSTAINED crossing (default 2 min) with hysteresis, so these are not
  // chatty — a spike from sun on the enclosure will not reach here.
  // `severity` is 'warn' at the threshold, 'critical' once the Pi is actually
  // throttling or past its soft limit, and escalation re-notifies immediately
  // because "it is throttling now" is new information.
  'system.overheat': {
    title: 'Controller overheating',
    body: (p) => {
      const t = typeof p.tempC === 'number' ? `${p.tempC.toFixed(1)}°C` : 'unknown';
      const limit =
        typeof p.thresholdC === 'number' ? ` (warn ${p.thresholdC}°C)` : '';
      return p.severity === 'critical'
        ? `Pi at ${t}${limit} and THROTTLING — performance is already degraded. Check airflow.`
        : `Pi at ${t}${limit}. Rising; check airflow before it throttles.`;
    },
  },
  'system.overheat.cleared': {
    title: 'Controller temperature normal',
    body: (p) =>
      typeof p.tempC === 'number'
        ? `Pi back down to ${p.tempC.toFixed(1)}°C.`
        : 'Pi temperature back to normal.',
  },
};

// Send one Telegram message. Best-effort: logs and swallows failures so a
// bad chat ID or transient API error never fails the whole trigger.
async function sendTelegram(
  token: string,
  chatId: string,
  text: string,
): Promise<void> {
  try {
    const resp = await fetch(
      `https://api.telegram.org/bot${token}/sendMessage`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: chatId, text, parse_mode: 'HTML' }),
      },
    );
    if (!resp.ok) {
      const detail = await resp.text().catch(() => '');
      logger.error(`telegram send to ${chatId} failed: ${resp.status} ${detail}`);
    }
  } catch (err) {
    logger.error(`telegram send to ${chatId} threw`, err);
  }
}

async function fanoutTelegram(
  unitId: string,
  copy: { title: string; body: (p: Record<string, unknown>) => string },
  payload: Record<string, unknown>,
): Promise<void> {
  const token = TELEGRAM_BOT_TOKEN.value();
  if (!token) {
    logger.info('telegram: no bot token configured, skipping');
    return;
  }
  const cfgSnap = await db.doc(`units/${unitId}/config/notifications`).get();
  const chatIds = (cfgSnap.get('telegramChatIds') as string[] | undefined) ?? [];
  if (chatIds.length === 0) {
    logger.info(`telegram: no chat IDs configured for ${unitId}, skipping`);
    return;
  }
  const text = `<b>${copy.title}</b>\n${copy.body(payload)}`;
  await Promise.all(chatIds.map((id) => sendTelegram(token, id, text)));
}

async function fanoutExpoPush(
  unitId: string,
  eventId: string,
  kind: string,
  copy: { title: string; body: (p: Record<string, unknown>) => string },
  payload: Record<string, unknown>,
): Promise<void> {
  // Find the owner of this unit.
  const unitDoc = await db.doc(`units/${unitId}`).get();
  const ownerId = unitDoc.get('ownerId') as string | null;
  if (!ownerId) {
    logger.warn(`event/${kind} on unit ${unitId} — no owner, skipping push`);
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
      body: copy.body(payload),
      data: { unitId, eventId, kind },
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
}

export const onEventCreated = onDocumentCreated(
  { document: 'units/{unitId}/events/{eventId}', secrets: [TELEGRAM_BOT_TOKEN] },
  async (event) => {
    const snap = event.data;
    if (!snap) return;
    const data = snap.data() as { kind?: string; payload?: Record<string, unknown> };
    const unitId = event.params.unitId;
    const kind = data.kind ?? '';
    const payload = data.payload ?? {};

    const copy = COPY[kind];
    if (!copy) {
      logger.info(`event/${kind} — no notification copy registered, skipping`);
      return;
    }

    // Expo push and Telegram are independent channels — a missing owner or
    // no push tokens must not prevent the Telegram message, and vice versa.
    await Promise.all([
      fanoutExpoPush(unitId, event.params.eventId, kind, copy, payload),
      fanoutTelegram(unitId, copy, payload),
    ]);
  },
);
