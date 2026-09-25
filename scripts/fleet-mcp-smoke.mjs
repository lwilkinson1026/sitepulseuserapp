// Smoke test for the fleet MCP server — connects exactly like an MCP client
// (e.g. Grok via the xAI API) would, lists the tools, and runs read-only
// calls plus one deliberately refused control call. Never moves hardware.
//
// Usage:
//   node scripts/fleet-mcp-smoke.mjs
//   FLEET_MCP_URL=https://… TOKEN_FILE=~/.sitepulse-fleet-mcp-token node scripts/fleet-mcp-smoke.mjs

import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { createRequire } from 'node:module';

// The MCP SDK is a dependency of functions/, not the app — borrow it.
const require = createRequire(new URL('../functions/package.json', import.meta.url));
const { Client } = require('@modelcontextprotocol/sdk/client/index.js');
const { StreamableHTTPClientTransport } = require('@modelcontextprotocol/sdk/client/streamableHttp.js');

const URL_ = process.env.FLEET_MCP_URL ?? 'https://us-central1-sitepulse-userapp.cloudfunctions.net/fleetMcp';
const tokenFile = (process.env.TOKEN_FILE ?? '~/.sitepulse-fleet-mcp-token').replace(/^~/, homedir());
const token = readFileSync(tokenFile, 'utf-8').trim();

const client = new Client({ name: 'fleet-mcp-smoke', version: '1.0.0' });
await client.connect(
  new StreamableHTTPClientTransport(new URL(URL_), {
    requestInit: { headers: { Authorization: `Bearer ${token}` } },
  }),
);

const { tools } = await client.listTools();
console.log(`tools (${tools.length}): ${tools.map((t) => t.name).join(', ')}`);

const text = (r) => r.content?.map((c) => c.text).join('\n') ?? '';

const units = JSON.parse(text(await client.callTool({ name: 'list_units', arguments: {} })));
console.log('\nlist_units:');
console.log(`  botControlEnabled=${units.botControlEnabled}`);
for (const u of units.units) {
  console.log(`  ${u.unitId}: access=${u.access} online=${u.online} age=${u.telemetryAgeSec}s ` +
    `soc=${u.batterySocPct} packV=${u.packVolts} engine=${u.engineState}`);
}

if (units.units[0]) {
  const s = await client.callTool({ name: 'get_unit_status', arguments: { unitId: units.units[0].unitId } });
  console.log(`\nget_unit_status(${units.units[0].unitId}): ${s.isError ? 'ERROR' : 'ok'}, ${text(s).length} chars`);
}

// Must be refused (unknown unit) — proves the guard path without touching hardware.
const r = await client.callTool({
  name: 'start_engine',
  arguments: { unitId: 'UNIT-DOES-NOT-EXIST', reason: 'smoke test: expect refusal', waitSec: 0 },
});
console.log(`\nstart_engine on bogus unit → isError=${r.isError}: ${text(r)}`);

await client.close();
