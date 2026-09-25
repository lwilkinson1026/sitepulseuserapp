// Fleet MCP server — lets an external AI agent (a Grok bot via the xAI API)
// watch and operate every SitePulse unit.
//
// Transport: MCP Streamable HTTP, stateless, JSON responses. One POST = one
// JSON-RPC exchange, which fits a Cloud Function (no sessions, no SSE).
//
// It is a translation layer, not a new control path: control tools write the
// same units/{unitId}/commands docs the app writes (issuedBy: 'bot:grok') and
// wait for the Pi's ack; read tools read the same telemetry the app reads. All
// hardware-level safety (current clamps, catch detection, low-voltage aborts)
// stays on the Pi.
//
// What this layer adds — enforced in code, never left to the model's prompt:
//   • Auth: a bearer token only the bot holds (secret FLEET_MCP_TOKEN).
//   • Kill switch + per-unit access in fleet/bot:
//       { enabled, defaultAccess: 'full'|'read'|'off',
//         units: { 'UNIT-002': 'read', … },
//         maxEngineStartsPerHour, staleAfterSec }
//     enabled=false blocks every control tool; reads keep working.
//   • Refuses control on units whose telemetry is stale (except stop_engine).
//   • Rate limit on engine starts per unit.
//   • Every control call requires a `reason`, is written to fleetBotAudit, and
//     engine/AC/sentry actions post a `bot.command` event so the owner gets a
//     push / Telegram alert.

import { onRequest } from 'firebase-functions/v2/https';
import { defineSecret } from 'firebase-functions/params';
import { logger } from 'firebase-functions';
import * as admin from 'firebase-admin';
import { timingSafeEqual } from 'node:crypto';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { z } from 'zod';

if (!admin.apps.length) {
  admin.initializeApp();
}

const db = admin.firestore();
const FLEET_MCP_TOKEN = defineSecret('FLEET_MCP_TOKEN');

const BOT_ID = 'bot:grok';

// ─── bot policy (fleet/bot) ─────────────────────────────────────────────────

type Access = 'full' | 'read' | 'off';

type BotPolicy = {
  enabled: boolean;
  defaultAccess: Access;
  units: Record<string, Access>;
  maxEngineStartsPerHour: number;
  staleAfterSec: number;
};

// Missing doc = bot disabled. Turning the bot on is a deliberate act.
const DEFAULT_POLICY: BotPolicy = {
  enabled: false,
  defaultAccess: 'read',
  units: {},
  maxEngineStartsPerHour: 3,
  staleAfterSec: 180,
};

async function loadPolicy(): Promise<BotPolicy> {
  const snap = await db.doc('fleet/bot').get();
  return { ...DEFAULT_POLICY, ...(snap.data() as Partial<BotPolicy> | undefined) };
}

function accessFor(policy: BotPolicy, unitId: string): Access {
  return policy.units?.[unitId] ?? policy.defaultAccess;
}

// ─── helpers ────────────────────────────────────────────────────────────────

class ToolRefusal extends Error {}

type ToolResult = { content: { type: 'text'; text: string }[]; isError?: boolean };

function ok(data: unknown): ToolResult {
  return { content: [{ type: 'text', text: JSON.stringify(data, jsonSafe, 2) }] };
}

function refuse(message: string): ToolResult {
  return { content: [{ type: 'text', text: `REFUSED: ${message}` }], isError: true };
}

// Firestore Timestamps → ISO strings so the model sees readable times.
function jsonSafe(_key: string, value: unknown): unknown {
  if (value && typeof value === 'object' && 'toDate' in value &&
      typeof (value as { toDate: unknown }).toDate === 'function') {
    return (value as admin.firestore.Timestamp).toDate().toISOString();
  }
  if (value && typeof value === 'object' && '_seconds' in value && '_nanoseconds' in value) {
    const v = value as { _seconds: number; _nanoseconds: number };
    return new Date(v._seconds * 1000 + v._nanoseconds / 1e6).toISOString();
  }
  return value;
}

async function unitExists(unitId: string): Promise<boolean> {
  return (await db.doc(`units/${unitId}`).get()).exists;
}

async function telemetry(unitId: string) {
  const [snap, engine] = await Promise.all([
    db.doc(`units/${unitId}/current/snapshot`).get(),
    db.doc(`units/${unitId}/current/engine`).get(),
  ]);
  const s = snap.data() ?? null;
  const last = s?.last_update as admin.firestore.Timestamp | undefined;
  const ageSec = last ? Math.round((Date.now() - last.toMillis()) / 1000) : null;
  if (s) delete s.raw_frame_hex; // decoder debugging noise
  return { snapshot: s, engine: engine.data() ?? null, ageSec };
}

async function requireReadable(policy: BotPolicy, unitId: string) {
  if (!(await unitExists(unitId))) throw new ToolRefusal(`Unknown unit ${unitId}. Use list_units.`);
  if (accessFor(policy, unitId) === 'off') throw new ToolRefusal(`Bot access to ${unitId} is off.`);
}

/**
 * Gate for every control tool. `allowStale` is for actions in the safe
 * direction (stopping the engine) that should go out even if the unit
 * looks offline — the Pi will act on it when it reconnects.
 */
async function requireControllable(
  policy: BotPolicy,
  unitId: string,
  opts: { allowStale?: boolean } = {},
) {
  if (!policy.enabled) throw new ToolRefusal('Bot control is disabled fleet-wide (fleet/bot.enabled = false).');
  await requireReadable(policy, unitId);
  if (accessFor(policy, unitId) !== 'full') {
    throw new ToolRefusal(`Bot has read-only access to ${unitId}.`);
  }
  if (!opts.allowStale) {
    const { ageSec } = await telemetry(unitId);
    if (ageSec === null || ageSec > policy.staleAfterSec) {
      throw new ToolRefusal(
        `${unitId} telemetry is ${ageSec === null ? 'missing' : `${ageSec}s old`} ` +
          `(limit ${policy.staleAfterSec}s). Refusing to act on a unit that may be offline.`,
      );
    }
  }
}

async function enforceStartRateLimit(policy: BotPolicy, unitId: string) {
  const since = Date.now() - 60 * 60 * 1000;
  // Recent commands only, filtered in memory: avoids needing a composite index.
  const recent = await db
    .collection(`units/${unitId}/commands`)
    .orderBy('issuedAt', 'desc')
    .limit(100)
    .get();
  const starts = recent.docs.filter((d) => {
    const at = d.get('issuedAt') as admin.firestore.Timestamp | undefined;
    return d.get('issuedBy') === BOT_ID &&
      ['engine.start', 'engine.charge'].includes(d.get('kind')) &&
      at && at.toMillis() >= since;
  }).length;
  if (starts >= policy.maxEngineStartsPerHour) {
    throw new ToolRefusal(
      `Rate limit: the bot already issued ${starts} engine start/charge commands to ${unitId} ` +
        `in the last hour (max ${policy.maxEngineStartsPerHour}). Investigate before retrying.`,
    );
  }
}

// Actions the owner should hear about immediately.
const NOTIFY_KINDS = new Set([
  'engine.start', 'engine.stop', 'engine.charge', 'engine.override',
  'ac.toggle', 'sentry.disarm', 'charge.update',
]);

/**
 * Write the command, wait for the Pi to ack or fail it, audit everything.
 * Returns the final command state; a timeout is not an error — the command
 * stays pending and get_command_result can check it later.
 */
async function issue(
  tool: string,
  unitId: string,
  kind: string,
  payload: Record<string, unknown>,
  reason: string,
  waitSec: number,
): Promise<ToolResult> {
  const ref = await db.collection(`units/${unitId}/commands`).add({
    kind,
    issuedBy: BOT_ID,
    issuedAt: admin.firestore.FieldValue.serverTimestamp(),
    payload,
    status: 'pending',
    reason,
  });

  if (NOTIFY_KINDS.has(kind)) {
    await db.collection(`units/${unitId}/events`).add({
      kind: 'bot.command',
      at: admin.firestore.FieldValue.serverTimestamp(),
      source: 'cloud',
      payload: { tool, command: kind, reason, commandId: ref.id },
    }).catch((err) => logger.warn('[fleetMcp] event write failed', err));
  }

  const deadline = Date.now() + waitSec * 1000;
  let doc = await ref.get();
  while (doc.get('status') === 'pending' && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 1000));
    doc = await ref.get();
  }

  const status = doc.get('status') as string;
  await audit({ tool, unitId, kind, payload, reason, commandId: ref.id, outcome: status, error: doc.get('error') ?? null });

  const engine = kind.startsWith('engine.') ? (await telemetry(unitId)).engine : undefined;
  return ok({
    commandId: ref.id,
    status, // 'ack' | 'failed' | 'pending' (not answered within waitSec)
    error: doc.get('error') ?? undefined,
    note: status === 'pending'
      ? `No answer from the unit within ${waitSec}s. It may be offline or busy; check with get_command_result.`
      : undefined,
    engineStateNow: engine,
  });
}

async function audit(entry: Record<string, unknown>) {
  await db.collection('fleetBotAudit').add({
    ...entry,
    bot: BOT_ID,
    at: admin.firestore.FieldValue.serverTimestamp(),
  }).catch((err) => logger.error('[fleetMcp] audit write failed', err));
}

// Wrap each handler: load policy once, turn ToolRefusal into a clean refusal
// the model can read (and audit it), and never leak stack traces.
function guarded<A>(tool: string, fn: (args: A, policy: BotPolicy) => Promise<ToolResult>) {
  return async (args: A): Promise<ToolResult> => {
    try {
      return await fn(args, await loadPolicy());
    } catch (err) {
      if (err instanceof ToolRefusal) {
        const a = args as { unitId?: string; reason?: string };
        await audit({ tool, unitId: a.unitId ?? null, reason: a.reason ?? null, outcome: 'refused', error: err.message });
        return refuse(err.message);
      }
      logger.error(`[fleetMcp] ${tool} failed`, err);
      return refuse(`Internal error in ${tool}. The owner has been logged the details.`);
    }
  };
}

// ─── server ─────────────────────────────────────────────────────────────────

const unitId = z.string().regex(/^[A-Za-z0-9_-]{1,64}$/).describe('Unit id, e.g. "UNIT-002". Get ids from list_units.');
const reason = z.string().min(3).max(500).describe('Why you are doing this, in one sentence. Logged and sent to the owner.');
const waitSec = z.number().int().min(0).max(90).default(30)
  .describe('Seconds to wait for the unit to confirm (0 = fire and forget).');

const READ = { readOnlyHint: true, openWorldHint: false } as const;
const WRITE = { readOnlyHint: false, destructiveHint: false, openWorldHint: true } as const;

function buildServer(): McpServer {
  const server = new McpServer(
    { name: 'sitepulse-fleet', version: '1.0.0' },
    {
      instructions:
        'Tools to monitor and operate SitePulse hybrid generator units (battery pack + gas engine ' +
        'that recharges it). Always call get_unit_status before a control action and check the ' +
        'result afterwards. Engine starts are physical, noisy and burn fuel — only start when the ' +
        'battery needs it. Stopping the engine is always the safe direction. A REFUSED result is a ' +
        'policy decision; do not try to work around it, report it.',
    },
  );

  // ── read ──
  server.registerTool('list_units', {
    title: 'List units',
    description: 'Every unit in the fleet with online status, battery, engine state and the bot\'s access level.',
    annotations: READ,
  }, guarded('list_units', async (_args, policy) => {
    const units = await db.collection('units').get();
    const rows = await Promise.all(units.docs.map(async (u) => {
      const access = accessFor(policy, u.id);
      if (access === 'off') return null;
      const { snapshot: s, engine, ageSec } = await telemetry(u.id);
      return {
        unitId: u.id,
        model: u.get('model'),
        timezone: u.get('regionTimezone'),
        access,
        online: ageSec !== null && ageSec <= policy.staleAfterSec,
        telemetryAgeSec: ageSec,
        batterySocPct: s?.battery_soc ?? null,
        packVolts: s?.motor_volts ?? null,
        outputWatts: s?.output_watts ?? null,
        acOn: s?.ac_active ?? null,
        engineState: engine?.state ?? 'unknown',
      };
    }));
    return ok({ botControlEnabled: policy.enabled, units: rows.filter(Boolean) });
  }));

  server.registerTool('get_unit_status', {
    title: 'Unit status',
    description: 'Full live telemetry for one unit: battery, output, pack voltage, motor/engine state, temperatures, telemetry age.',
    inputSchema: { unitId },
    annotations: READ,
  }, guarded('get_unit_status', async ({ unitId }, policy) => {
    await requireReadable(policy, unitId);
    const t = await telemetry(unitId);
    return ok({ unitId, access: accessFor(policy, unitId), telemetryAgeSec: t.ageSec,
      online: t.ageSec !== null && t.ageSec <= policy.staleAfterSec, snapshot: t.snapshot, engine: t.engine });
  }));

  server.registerTool('get_events', {
    title: 'Recent events',
    description: 'Recent event log for a unit (engine starts/stops, low battery, overheating, motion, fuel, bot actions), newest first.',
    inputSchema: {
      unitId,
      limit: z.number().int().min(1).max(50).default(20),
      kinds: z.array(z.string()).optional().describe('Only these kinds, e.g. ["engine.start","system.overheat"].'),
    },
    annotations: READ,
  }, guarded('get_events', async ({ unitId, limit, kinds }, policy) => {
    await requireReadable(policy, unitId);
    const snap = await db.collection(`units/${unitId}/events`).orderBy('at', 'desc').limit(kinds ? 200 : limit).get();
    const events = snap.docs
      .map((d) => ({ id: d.id, ...d.data() }))
      .filter((e) => !kinds || kinds.includes((e as { kind?: string }).kind ?? ''))
      .slice(0, limit);
    return ok({ unitId, events });
  }));

  server.registerTool('get_config', {
    title: 'Unit config',
    description: 'Read a unit configuration document.',
    inputSchema: {
      unitId,
      name: z.enum(['charge', 'engine', 'sentry', 'fan', 'relays', 'camera']).describe(
        'charge = recharge schedule/windows/quiet hours; engine = supervisor voltage thresholds and crank/charge settings.'),
    },
    annotations: READ,
  }, guarded('get_config', async ({ unitId, name }, policy) => {
    await requireReadable(policy, unitId);
    const doc = await db.doc(`units/${unitId}/config/${name}`).get();
    return ok({ unitId, name, config: doc.data() ?? null });
  }));

  server.registerTool('get_command_result', {
    title: 'Command result',
    description: 'Check the status of a command issued earlier (pending / ack / failed).',
    inputSchema: { unitId, commandId: z.string().min(1).max(128) },
    annotations: READ,
  }, guarded('get_command_result', async ({ unitId, commandId }, policy) => {
    await requireReadable(policy, unitId);
    const doc = await db.doc(`units/${unitId}/commands/${commandId}`).get();
    if (!doc.exists) throw new ToolRefusal(`No command ${commandId} on ${unitId}.`);
    return ok({ commandId, ...doc.data() });
  }));

  // ── engine ──
  server.registerTool('start_engine', {
    title: 'Start engine',
    description: 'Run the full engine start sequence (spark on, crank, settle). Rate-limited per unit. ' +
      'Does not start charging by itself — use charge_engine for that.',
    inputSchema: { unitId, reason, waitSec },
    annotations: WRITE,
  }, guarded('start_engine', async ({ unitId, reason, waitSec }, policy) => {
    await requireControllable(policy, unitId);
    await enforceStartRateLimit(policy, unitId);
    return issue('start_engine', unitId, 'engine.start', {}, reason, waitSec);
  }));

  server.registerTool('charge_engine', {
    title: 'Charge battery from engine',
    description: 'Start the regen charge loop (engine drives the motor as a generator to charge the pack). ' +
      'The unit enforces its own current/voltage/temperature limits and stops at the configured voltage.',
    inputSchema: {
      unitId, reason, waitSec,
      currentAmps: z.number().positive().max(100).optional().describe('Charge current; unit clamps to its hardware ceiling. Omit for the configured default.'),
      maxDurationSec: z.number().int().positive().max(6 * 3600).optional(),
    },
    annotations: WRITE,
  }, guarded('charge_engine', async ({ unitId, reason, waitSec, currentAmps, maxDurationSec }, policy) => {
    await requireControllable(policy, unitId);
    await enforceStartRateLimit(policy, unitId);
    const payload: Record<string, unknown> = {};
    if (currentAmps !== undefined) payload.currentAmpsOverride = currentAmps;
    if (maxDurationSec !== undefined) payload.maxDurationSecOverride = maxDurationSec;
    return issue('charge_engine', unitId, 'engine.charge', payload, reason, waitSec);
  }));

  server.registerTool('tune_charge', {
    title: 'Adjust charge current',
    description: 'Change the target current of a charge loop that is already running.',
    inputSchema: { unitId, reason, waitSec, currentAmps: z.number().positive().max(100) },
    annotations: WRITE,
  }, guarded('tune_charge', async ({ unitId, reason, waitSec, currentAmps }, policy) => {
    await requireControllable(policy, unitId);
    return issue('tune_charge', unitId, 'engine.charge.tune', { currentAmps }, reason, waitSec);
  }));

  server.registerTool('stop_engine', {
    title: 'Stop engine',
    description: 'Gracefully stop the engine and any charge loop. Always allowed, even if the unit looks offline.',
    inputSchema: { unitId, reason, waitSec },
    annotations: { ...WRITE, destructiveHint: false, idempotentHint: true },
  }, guarded('stop_engine', async ({ unitId, reason, waitSec }, policy) => {
    await requireControllable(policy, unitId, { allowStale: true });
    return issue('stop_engine', unitId, 'engine.stop', {}, reason, waitSec);
  }));

  server.registerTool('override_schedule', {
    title: 'Override charge schedule',
    description: 'One-off override of the automatic recharge scheduler: run_now (charge now even outside a window), ' +
      'stop (stop the current scheduled run), allow_quiet_once (permit one run during quiet hours).',
    inputSchema: { unitId, reason, waitSec, action: z.enum(['run_now', 'stop', 'allow_quiet_once']) },
    annotations: WRITE,
  }, guarded('override_schedule', async ({ unitId, reason, waitSec, action }, policy) => {
    await requireControllable(policy, unitId, { allowStale: action === 'stop' });
    if (action === 'run_now') await enforceStartRateLimit(policy, unitId);
    return issue('override_schedule', unitId, 'engine.override', { action }, reason, waitSec);
  }));

  server.registerTool('update_charge_schedule', {
    title: 'Update charge schedule',
    description: 'Change the automatic recharge schedule. Only the fields you pass are changed.',
    inputSchema: {
      unitId, reason, waitSec,
      enabled: z.boolean().optional(),
      preset: z.enum(['daytime_only', 'eco', 'quiet_off', 'storm', 'custom']).optional(),
      windows: z.array(z.object({
        start: z.string().regex(/^\d{2}:\d{2}$/),
        end: z.string().regex(/^\d{2}:\d{2}$/),
        weekdays: z.array(z.number().int().min(0).max(6)),
      })).max(14).optional().describe('Allowed charge windows, unit-local time; weekdays 0 = Sunday.'),
      quietHours: z.object({
        start: z.string().regex(/^\d{2}:\d{2}$/),
        end: z.string().regex(/^\d{2}:\d{2}$/),
      }).optional(),
      allowQuietOverride: z.boolean().optional(),
    },
    annotations: WRITE,
  }, guarded('update_charge_schedule', async ({ unitId, reason, waitSec, ...patch }, policy) => {
    await requireControllable(policy, unitId);
    const configPatch = Object.fromEntries(Object.entries(patch).filter(([, v]) => v !== undefined));
    if (Object.keys(configPatch).length === 0) throw new ToolRefusal('Nothing to change.');
    return issue('update_charge_schedule', unitId, 'charge.update', { configPatch }, reason, waitSec);
  }));

  // ── inverter panel ──
  server.registerTool('toggle_ac', {
    title: 'Toggle AC outlets',
    description: 'Press the inverter\'s AC button (toggles AC output on/off). The reported acOn state can lag or be ' +
      'wrong while the panel display is asleep — call wake_lcd first and re-check status after.',
    inputSchema: { unitId, reason, waitSec },
    annotations: WRITE,
  }, guarded('toggle_ac', async ({ unitId, reason, waitSec }, policy) => {
    await requireControllable(policy, unitId);
    return issue('toggle_ac', unitId, 'ac.toggle', {}, reason, waitSec);
  }));

  server.registerTool('wake_lcd', {
    title: 'Wake panel display',
    description: 'Press the inverter\'s display button so it wakes and battery/output readings refresh.',
    inputSchema: { unitId, reason, waitSec },
    annotations: WRITE,
  }, guarded('wake_lcd', async ({ unitId, reason, waitSec }, policy) => {
    await requireControllable(policy, unitId);
    return issue('wake_lcd', unitId, 'lcd.wake', {}, reason, waitSec);
  }));

  server.registerTool('set_fan', {
    title: 'Set cooling fan',
    description: 'Cooling fan speed: auto (follows engine state) or manual with a speed.',
    inputSchema: {
      unitId, reason, waitSec,
      mode: z.enum(['auto', 'manual']),
      speedPct: z.number().int().min(0).max(100).optional().describe('Required for manual.'),
    },
    annotations: WRITE,
  }, guarded('set_fan', async ({ unitId, reason, waitSec, mode, speedPct }, policy) => {
    await requireControllable(policy, unitId);
    if (mode === 'manual' && speedPct === undefined) throw new ToolRefusal('speedPct is required for manual mode.');
    return issue('set_fan', unitId, 'fan.set', mode === 'manual' ? { mode, speedPct } : { mode }, reason, waitSec);
  }));

  // ── security ──
  server.registerTool('arm_sentry', {
    title: 'Arm sentry',
    description: 'Arm motion-triggered camera recording.',
    inputSchema: { unitId, reason, waitSec },
    annotations: WRITE,
  }, guarded('arm_sentry', async ({ unitId, reason, waitSec }, policy) => {
    await requireControllable(policy, unitId);
    return issue('arm_sentry', unitId, 'sentry.arm', {}, reason, waitSec);
  }));

  server.registerTool('disarm_sentry', {
    title: 'Disarm sentry',
    description: 'Disarm motion-triggered camera recording.',
    inputSchema: { unitId, reason, waitSec },
    annotations: WRITE,
  }, guarded('disarm_sentry', async ({ unitId, reason, waitSec }, policy) => {
    await requireControllable(policy, unitId);
    return issue('disarm_sentry', unitId, 'sentry.disarm', {}, reason, waitSec);
  }));

  server.registerTool('camera_stream', {
    title: 'Camera live stream',
    description: 'Start or stop the unit\'s live camera stream (uses cellular/Starlink data while on).',
    inputSchema: { unitId, reason, waitSec, action: z.enum(['start', 'stop']) },
    annotations: WRITE,
  }, guarded('camera_stream', async ({ unitId, reason, waitSec, action }, policy) => {
    await requireControllable(policy, unitId, { allowStale: action === 'stop' });
    return issue('camera_stream', unitId, action === 'start' ? 'camera.startStream' : 'camera.stopStream', {}, reason, waitSec);
  }));

  return server;
}

// ─── HTTP entry ─────────────────────────────────────────────────────────────

function authorized(header: string | undefined): boolean {
  const expected = FLEET_MCP_TOKEN.value();
  // xAI passes the tool's `authorization` value through as the header; accept
  // it with or without the "Bearer " prefix so either config works.
  const got = (header ?? '').replace(/^Bearer\s+/i, '').trim();
  if (!expected || !got) return false;
  const a = Buffer.from(got);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

export const fleetMcp = onRequest(
  {
    secrets: [FLEET_MCP_TOKEN],
    invoker: 'public',        // auth is the bearer token below
    timeoutSeconds: 120,      // control tools wait up to 90 s for the Pi
    concurrency: 20,
  },
  async (req, res) => {
    if (!authorized(req.headers.authorization)) {
      res.status(401).json({ jsonrpc: '2.0', error: { code: -32001, message: 'Unauthorized' }, id: null });
      return;
    }
    if (req.method !== 'POST') {
      // Stateless server: no SSE stream to open, no session to delete.
      res.status(405).set('Allow', 'POST').json({ jsonrpc: '2.0', error: { code: -32000, message: 'Method not allowed' }, id: null });
      return;
    }

    const server = buildServer();
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
      enableJsonResponse: true,
    });
    res.on('close', () => {
      transport.close().catch(() => {});
      server.close().catch(() => {});
    });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
    } catch (err) {
      logger.error('[fleetMcp] request failed', err);
      if (!res.headersSent) {
        res.status(500).json({ jsonrpc: '2.0', error: { code: -32603, message: 'Internal error' }, id: null });
      }
    }
  },
);
