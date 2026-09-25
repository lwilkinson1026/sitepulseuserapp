// Grok alerts — wake the Grok fleet operator when a unit needs attention.
//
// Four detectors, each edge-triggered (fires once per incident, re-arms only
// after the condition clears) with its state in units/{unitId}/alerts/state:
//
//   battery_low    SoC ≤ 10 % (or, with the display asleep, pack ≤ 3.10 V/cell
//                  at rest) for 2 min. Clears at SoC ≥ 15 % / ≥ 3.20 V/cell.
//   pi_hot         Pi SoC temp ≥ its warn threshold (75 °C) for 2 min, or the
//                  Pi reports it is thermally throttling right now. Clears
//                  5 °C below the threshold.
//   engine_bogged  The 2nd failed_engine_bogged within 6 h. Re-arms after
//                  that window.
//   offline        No telemetry for 30 min (scheduled check every 5 min).
//                  Clears when telemetry resumes.
//
// On fire: write an event (so the owner's push/Telegram + Activity log show
// it), then call the xAI Responses API with the fleet MCP server attached so
// Grok inspects the unit and acts under the usual server-side guardrails, and
// send Grok's report to the escalation Telegram chats (Landon only — NOT the
// unit's notification chats, which a customer may share).
//
// Knobs live in fleet/bot (same doc as the MCP policy):
//   alertsEnabled (default true), escalationTelegramChatIds (default []),
//   grokModel (default 'grok-4.7'), maxWakesPerDay (default 24),
//   lowSocPct (10), clearSocPct (15), offlineAfterMin (30)

import { onDocumentWritten } from 'firebase-functions/v2/firestore';
import { onSchedule } from 'firebase-functions/v2/scheduler';
import { defineSecret } from 'firebase-functions/params';
import { logger } from 'firebase-functions';
import * as admin from 'firebase-admin';
import { GROK_SYSTEM_PROMPT } from './grokPrompt';

if (!admin.apps.length) {
  admin.initializeApp();
}

const db = admin.firestore();
const FieldValue = admin.firestore.FieldValue;

const XAI_API_KEY = defineSecret('XAI_API_KEY');
const FLEET_MCP_TOKEN = defineSecret('FLEET_MCP_TOKEN');
const TELEGRAM_BOT_TOKEN = defineSecret('TELEGRAM_BOT_TOKEN');
const SECRETS = [XAI_API_KEY, FLEET_MCP_TOKEN, TELEGRAM_BOT_TOKEN];

const FLEET_MCP_URL = 'https://us-central1-sitepulse-userapp.cloudfunctions.net/fleetMcp';

const SUSTAIN_MS = 2 * 60 * 1000;          // battery / heat must persist this long
const BOG_WINDOW_MS = 6 * 60 * 60 * 1000;  // two bogs within this window = alert
const GROK_TIMEOUT_MS = 8 * 60 * 1000;     // leave headroom under the 540 s limit

type AlertKind = 'battery_low' | 'pi_hot' | 'engine_bogged' | 'offline';

type AlertPolicy = {
  alertsEnabled: boolean;
  escalationTelegramChatIds: string[];
  grokModel: string;
  maxWakesPerDay: number;
  lowSocPct: number;
  clearSocPct: number;
  offlineAfterMin: number;
};

const DEFAULTS: AlertPolicy = {
  alertsEnabled: true,
  escalationTelegramChatIds: [],
  grokModel: 'grok-4.7',
  maxWakesPerDay: 24,
  lowSocPct: 10,
  clearSocPct: 15,
  offlineAfterMin: 30,
};

async function loadPolicy(): Promise<AlertPolicy> {
  const snap = await db.doc('fleet/bot').get();
  return { ...DEFAULTS, ...(snap.data() as Partial<AlertPolicy> | undefined) };
}

// ─── detector state ─────────────────────────────────────────────────────────

type DetectorState = { active?: boolean; pendingSince?: number | null };
type AlertState = {
  battery_low?: DetectorState;
  pi_hot?: DetectorState;
  offline?: DetectorState;
  bogTimes?: number[];
  recentReports?: { at: number; kind: string; report: string }[];
};

const stateRef = (unitId: string) => db.doc(`units/${unitId}/alerts/state`);

/**
 * Sustained edge detector. `raw` is the instantaneous reading:
 * true = bad, false = clearly recovered, null = in the hysteresis band or
 * unknown (keep whatever we had). Returns true exactly once per incident.
 * Runs in a transaction so concurrent snapshot writes can't double-fire.
 */
async function edge(
  unitId: string,
  key: 'battery_low' | 'pi_hot',
  raw: boolean | null,
  sustainMs: number,
): Promise<boolean> {
  return db.runTransaction(async (tx) => {
    const ref = stateRef(unitId);
    const cur = ((await tx.get(ref)).data() as AlertState | undefined)?.[key] ?? {};
    const now = Date.now();
    let next: DetectorState = { ...cur };
    let fire = false;

    if (raw === true) {
      if (!cur.active) {
        const since = cur.pendingSince ?? now;
        if (now - since >= sustainMs) {
          next = { active: true, pendingSince: null };
          fire = true;
        } else {
          next = { active: false, pendingSince: since };
        }
      }
    } else if (raw === false) {
      next = { active: false, pendingSince: null };
    } else {
      // Hysteresis band: an un-fired pending timer shouldn't survive it.
      if (!cur.active) next = { active: false, pendingSince: null };
    }

    const changed = next.active !== cur.active || next.pendingSince !== cur.pendingSince;
    if (changed) tx.set(ref, { [key]: next }, { merge: true });
    return fire;
  });
}

// ─── Grok wake-up ───────────────────────────────────────────────────────────

async function withinDailyBudget(max: number): Promise<boolean> {
  const day = new Date().toISOString().slice(0, 10);
  return db.runTransaction(async (tx) => {
    const ref = db.doc('fleet/wakeCounter');
    const cur = (await tx.get(ref)).data() as { day?: string; count?: number } | undefined;
    const count = cur?.day === day ? cur.count ?? 0 : 0;
    if (count >= max) return false;
    tx.set(ref, { day, count: count + 1 });
    return true;
  });
}

function responseText(body: unknown): string {
  const b = body as {
    output_text?: string;
    output?: { type?: string; content?: { type?: string; text?: string }[] }[];
  };
  if (typeof b.output_text === 'string' && b.output_text) return b.output_text;
  const parts: string[] = [];
  for (const item of b.output ?? []) {
    if (item.type !== 'message') continue;
    for (const c of item.content ?? []) if (c.text) parts.push(c.text);
  }
  return parts.join('\n').trim();
}

async function sendTelegram(chatIds: string[], text: string) {
  const token = TELEGRAM_BOT_TOKEN.value();
  if (!token || chatIds.length === 0) {
    logger.info('[grokAlerts] no escalation Telegram configured; report only in fleetBotWakes');
    return;
  }
  const body = text.length > 3900 ? text.slice(0, 3900) + '\n…(truncated)' : text;
  await Promise.all(chatIds.map(async (chatId) => {
    try {
      const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: chatId, text: body, disable_web_page_preview: true }),
      });
      if (!r.ok) logger.error(`[grokAlerts] telegram ${chatId}: ${r.status} ${await r.text().catch(() => '')}`);
    } catch (err) {
      logger.error(`[grokAlerts] telegram ${chatId} threw`, err);
    }
  }));
}

const TITLES: Record<AlertKind, string> = {
  battery_low: 'Battery below 10%',
  pi_hot: 'Controller (Raspberry Pi) running hot',
  engine_bogged: 'Engine bogged twice',
  offline: 'Unit offline',
};

async function wakeGrok(unitId: string, kind: AlertKind, details: Record<string, unknown>) {
  const policy = await loadPolicy();
  const started = Date.now();
  const wakeRef = db.collection('fleetBotWakes').doc();
  const base = { unitId, kind, details, at: FieldValue.serverTimestamp() };

  if (!policy.alertsEnabled) {
    await wakeRef.set({ ...base, outcome: 'skipped_disabled' });
    return;
  }
  if (!(await withinDailyBudget(policy.maxWakesPerDay))) {
    await wakeRef.set({ ...base, outcome: 'skipped_daily_cap' });
    await sendTelegram(policy.escalationTelegramChatIds,
      `⚠️ SitePulse: ${TITLES[kind]} on ${unitId}, but Grok's daily wake limit ` +
      `(${policy.maxWakesPerDay}) is used up. Check it yourself.\n${JSON.stringify(details)}`);
    return;
  }

  const recent = ((await stateRef(unitId).get()).data() as AlertState | undefined)?.recentReports ?? [];
  const history = recent.length
    ? '\n\nYour recent reports for this unit (oldest first):\n' +
      recent.map((r) => `- ${new Date(r.at).toISOString()} [${r.kind}] ${r.report.slice(0, 600)}`).join('\n')
    : '';

  const userMsg =
    `ALERT: ${TITLES[kind]} on ${unitId}.\n` +
    `Detected by the cloud monitor at ${new Date().toISOString()}. Details: ${JSON.stringify(details)}.\n` +
    `Focus on ${unitId}: check its status and recent events, act per your playbook, ` +
    `and end with your REPORT and, if a human is needed, ESCALATE lines.` + history;

  let report = '';
  let outcome = 'ok';
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), GROK_TIMEOUT_MS);
    const resp = await fetch('https://api.x.ai/v1/responses', {
      method: 'POST',
      signal: ctrl.signal,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${XAI_API_KEY.value()}`,
      },
      body: JSON.stringify({
        model: policy.grokModel,
        input: [
          { role: 'system', content: GROK_SYSTEM_PROMPT },
          { role: 'user', content: userMsg },
        ],
        tools: [{
          type: 'mcp',
          server_url: FLEET_MCP_URL,
          server_label: 'sitepulse_fleet',
          authorization: `Bearer ${FLEET_MCP_TOKEN.value()}`,
        }],
      }),
    }).finally(() => clearTimeout(timer));
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      outcome = `xai_error_${resp.status}`;
      report = JSON.stringify(body).slice(0, 1000);
    } else {
      report = responseText(body) || '(Grok returned no text)';
    }
  } catch (err) {
    outcome = 'xai_exception';
    report = err instanceof Error ? err.message : String(err);
  }

  const durationSec = Math.round((Date.now() - started) / 1000);
  await wakeRef.set({ ...base, outcome, report, durationSec, model: policy.grokModel });
  await stateRef(unitId).set({
    recentReports: [...recent, { at: Date.now(), kind, report: report.slice(0, 2000) }].slice(-3),
  }, { merge: true });

  const header = outcome === 'ok'
    ? `🤖 Grok handled: ${TITLES[kind]} — ${unitId}`
    : `⚠️ ${TITLES[kind]} — ${unitId}. Grok could NOT be reached (${outcome}); check it yourself.`;
  await sendTelegram(policy.escalationTelegramChatIds,
    `${header}\n${JSON.stringify(details)}\n\n${report}`);
  logger.info(`[grokAlerts] ${unitId} ${kind} → ${outcome} in ${durationSec}s`);
}

async function unitEvent(unitId: string, kind: string, payload: Record<string, unknown>) {
  await db.collection(`units/${unitId}/events`).add({
    kind, at: FieldValue.serverTimestamp(), source: 'cloud', payload,
  }).catch((err) => logger.warn(`[grokAlerts] event ${kind} write failed`, err));
}

// ─── detector 1 + 2: battery low, Pi hot (from live telemetry) ──────────────

type Snapshot = {
  battery_soc?: number | null;
  motor_volts?: number | null;
  motor_amps_in?: number | null;
  motor_stale?: boolean;
  pi_temp_c?: number | null;
  pi_temp_warn_c?: number;
  pi_throttled_now?: boolean;
};

async function cellCount(unitId: string): Promise<number | null> {
  const eng = await db.doc(`units/${unitId}/config/engine`).get();
  const n = eng.get('cellCount');
  return typeof n === 'number' && n > 0 ? n : null;
}

export const grokAlertOnTelemetry = onDocumentWritten(
  { document: 'units/{unitId}/current/snapshot', secrets: SECRETS, timeoutSeconds: 540 },
  async (event) => {
    const s = event.data?.after.data() as Snapshot | undefined;
    if (!s) return;
    const unitId = event.params.unitId;
    const policy = await loadPolicy();

    // Battery. Prefer the display's coulomb-counted SoC; fall back to resting
    // pack voltage only when the display is asleep (SoC null) and the pack is
    // not being charged (charging lifts terminal voltage 2–3 V).
    let low: boolean | null = null;
    let batteryDetails: Record<string, unknown> = {};
    if (typeof s.battery_soc === 'number') {
      low = s.battery_soc <= policy.lowSocPct ? true : s.battery_soc >= policy.clearSocPct ? false : null;
      batteryDetails = { batterySocPct: s.battery_soc, packVolts: s.motor_volts ?? null, source: 'display' };
    } else if (typeof s.motor_volts === 'number' && !s.motor_stale) {
      const charging = typeof s.motor_amps_in === 'number' && s.motor_amps_in < -1;
      const cells = await cellCount(unitId);
      if (!charging && cells) {
        const perCell = s.motor_volts / cells;
        low = perCell <= 3.10 ? true : perCell >= 3.20 ? false : null;
        batteryDetails = { batterySocPct: null, packVolts: s.motor_volts, voltsPerCell: +perCell.toFixed(3),
          source: 'pack_voltage (display asleep; approximate)' };
      }
    }
    if (low !== null || typeof s.battery_soc === 'number') {
      if (await edge(unitId, 'battery_low', low, SUSTAIN_MS)) {
        await unitEvent(unitId, 'battery.low', batteryDetails);
        await wakeGrok(unitId, 'battery_low', batteryDetails);
      }
    }

    // Pi temperature.
    if (typeof s.pi_temp_c === 'number') {
      const warn = typeof s.pi_temp_warn_c === 'number' ? s.pi_temp_warn_c : 75;
      const hot = s.pi_throttled_now === true || s.pi_temp_c >= warn
        ? true
        : s.pi_temp_c <= warn - 5 ? false : null;
      if (await edge(unitId, 'pi_hot', hot, s.pi_throttled_now ? 0 : SUSTAIN_MS)) {
        await wakeGrok(unitId, 'pi_hot', {
          piTempC: s.pi_temp_c, warnC: warn, throttlingNow: s.pi_throttled_now ?? null,
        });
      }
    }
  },
);

// ─── detector 3: engine bogged twice ────────────────────────────────────────

export const grokAlertOnEngine = onDocumentWritten(
  { document: 'units/{unitId}/current/engine', secrets: SECRETS, timeoutSeconds: 540 },
  async (event) => {
    const before = event.data?.before.data()?.state;
    const after = event.data?.after.data();
    if (after?.state !== 'failed_engine_bogged' || before === 'failed_engine_bogged') return;
    const unitId = event.params.unitId;

    const bogs = await db.runTransaction(async (tx) => {
      const ref = stateRef(unitId);
      const cur = ((await tx.get(ref)).data() as AlertState | undefined)?.bogTimes ?? [];
      const now = Date.now();
      const times = [...cur.filter((t) => now - t < BOG_WINDOW_MS), now];
      // Fire on exactly the 2nd bog in the window, then start counting afresh
      // so a 3rd/4th bog becomes the 1st/2nd of the next incident.
      const fire = times.length >= 2;
      tx.set(ref, { bogTimes: fire ? [] : times }, { merge: true });
      return fire ? times.length : 0;
    });

    if (bogs >= 2) {
      await wakeGrok(unitId, 'engine_bogged', {
        bogsInLast6h: bogs,
        finalRpm: after.finalRpm ?? null,
        durationSec: after.durationSec ?? null,
        currentAmpsCommanded: after.currentAmpsCommanded ?? null,
      });
    }
  },
);

// ─── detector 4: offline (scheduled) ────────────────────────────────────────

export const grokAlertOfflineSweep = onSchedule(
  { schedule: 'every 5 minutes', secrets: SECRETS, timeoutSeconds: 540 },
  async () => {
    const policy = await loadPolicy();
    const limitMs = policy.offlineAfterMin * 60 * 1000;
    const units = await db.collection('units').get();

    for (const u of units.docs) {
      const unitId = u.id;
      const snap = await db.doc(`units/${unitId}/current/snapshot`).get();
      const last = snap.get('last_update') as admin.firestore.Timestamp | undefined;
      const ageMs = last ? Date.now() - last.toMillis() : Infinity;
      const offline = ageMs > limitMs;

      const wasOffline = await db.runTransaction(async (tx) => {
        const ref = stateRef(unitId);
        const prev = ((await tx.get(ref)).data() as AlertState | undefined)?.offline?.active ?? false;
        if (prev !== offline) tx.set(ref, { offline: { active: offline } }, { merge: true });
        return prev;
      });

      if (offline && !wasOffline) {
        const details = {
          lastTelemetry: last ? last.toDate().toISOString() : null,
          silentForMin: Number.isFinite(ageMs) ? Math.round(ageMs / 60000) : null,
        };
        await unitEvent(unitId, 'system.offline', details);
        await wakeGrok(unitId, 'offline', details);
      } else if (!offline && wasOffline) {
        await unitEvent(unitId, 'system.online', {});
        await sendTelegram(policy.escalationTelegramChatIds, `✅ ${unitId} is back online.`);
      }
    }
  },
);
