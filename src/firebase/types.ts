// Firestore document shapes. These live with the client because the schema
// is mostly consumed by the app — the Pi publisher mirrors them in Python.
//
// Collection layout (spec §7):
//   units/{unitId}                 — unit metadata, ownership, lastSeen
//   units/{unitId}/current/snapshot — single telemetry doc, overwritten 2–5 s
//   units/{unitId}/history/{ts}    — hourly rollups (phase 2)
//   units/{unitId}/commands/{id}   — outlet/theft/schedule intents (phase 1)
//   units/{unitId}/events/{id}     — append-only event log

import type { Timestamp } from 'firebase/firestore';

export type SystemMode =
  | 'battery_only'
  | 'charging'
  | 'discharging'
  | 'fault'
  | 'idle';

// Top-level unit document. Written by the cloud (claimUnit / release flows)
// and by the Pi for lastSeen. Owner-readable; client cannot write directly.
export interface UnitDoc {
  serial: string;
  model: string;
  firmwareVersion: string;
  ownerId: string | null;          // null when unbound / awaiting claim
  bindingTokenHash?: string;       // hashed one-time-token, server-only
  theftFlag: boolean;
  geofence?: { lat: number; lng: number; radiusMeters: number };
  lastSeen: Timestamp | null;
  regionTimezone: string;          // IANA tz, e.g. "America/Denver"
}

// VESC Harmony 16: 16 cell voltages, multiple temperature sensors.
// units/{unitId}/current/snapshot — Pi overwrites this every 2–5 s.
// The app listens with onSnapshot and renders the dashboard.
//
// As of the Predator I²C migration (replacing the VESC CAN BMS pipeline),
// the snapshot reflects only what the Predator 2000W's LCD displays.  The
// Predator does not expose pack voltage, current, per-cell, or temperature
// over its bus — only the human-readable LCD fields below.  The full set
// of removed VESC-era fields is kept here as commented stubs in case we
// re-add a richer BMS path later.
export interface TelemetrySnapshot {
  // Battery — what the Predator LCD reports.  battery_soc is null when the
  // current (reg00, reg01) byte pair is not yet in the decoder's lookup
  // table; the publisher logs every unknown pair so the table grows over
  // normal use.  The scheduler safe-defaults to idle when soc is null.
  battery_soc: number | null;      // 0–100 % (or null if unmapped)

  // Output state derived from the LCD
  dc_active: boolean;              // DC outlet button enabled
  ac_active: boolean;              // AC outlet button enabled (heuristic)
  output_mode: 'AC' | 'DC' | 'AC+DC' | 'off';
  output_watts: number | null;     // current draw in watts (null if any digit unmapped)
  time_to_empty_minutes: number | null;  // remaining runtime, null when LCD shows the 99:59 placeholder

  // Optional / future
  bmsFaults?: string[];            // active fault codes (empty until LCD fault icons are mapped)
  outlets?: Record<string, boolean>;  // relay state, 1-indexed channels

  // Diagnostics
  lcd_frame_rate_hz?: number;      // observed I²C frame rate
  raw_frame_hex?: string;          // 18-byte hex dump, for ongoing decoder dev

  // VESC motor controller telemetry (phase G.1). All optional because the
  // Pi may publish before the CAN bus comes up, or with no controller wired.
  // `motor_stale` is the freshness indicator — true means no recent frame.
  motor_stale?: boolean;
  motor_age_sec?: number;          // seconds since last frame, 0 = just now
  motor_rpm?: number;              // signed; positive = drive, negative = reverse/regen
  motor_amps?: number;             // total motor current (A)
  motor_amps_in?: number;          // total DC input current to controller (A)
  motor_duty?: number;             // -1.0 to 1.0
  motor_volts?: number;            // input voltage at the controller
  motor_temp_c?: number;           // motor winding temp (probe must be wired)
  motor_fet_temp_c?: number;       // MOSFET / driver temp
  motor_tachometer?: number;       // cumulative count (units per VESC config)
  motor_ah?: number;               // cumulative amp-hours consumed
  motor_ah_charged?: number;       // cumulative amp-hours regenerated (charge output)
  motor_wh?: number;               // cumulative watt-hours consumed
  motor_wh_charged?: number;       // cumulative watt-hours regenerated

  // System
  system_mode: SystemMode;
  last_update: Timestamp;          // server timestamp set by the Pi
}

// units/{unitId}/commands/{id} — app writes, Pi consumes + ACKs.
// Naming convention: `<domain>.<verb>`. `*.update` overwrites the corresponding
// config doc; `*.set` toggles a single value on a current/* state doc.
export type CommandKind =
  // outlets (legacy AC outlet relays — phase 1)
  | 'outlet.toggle'
  // theft / geofence (phase 2)
  | 'theft.arm'
  | 'theft.disarm'
  // security light + waveshare relays (phase B)
  | 'light.set'
  | 'relay.set'
  // sentry + camera (phase D)
  | 'sentry.arm'
  | 'sentry.disarm'
  | 'sentry.update'
  | 'camera.startStream'
  | 'camera.stopStream'
  // engine recharge schedule (phase C)
  | 'engine.override'
  | 'charge.update'
  | 'schedule.update'
  // Predator LCD wake-button presser (phase E.2)
  | 'lcd.wake'
  // Predator AC outlet toggle-button presser (phase E.3)
  | 'ac.toggle'
  // VESC starter cranking (phase G.2)
  | 'engine.crank'
  // Full engine start choreography (phase G.3): choke → spark → crank → settle
  | 'engine.start'
  // Regen charging + graceful shutdown (phase G.4)
  | 'engine.charge'
  // Live tune of an already-running charge loop (phase G.4.1)
  | 'engine.charge.tune'
  | 'engine.stop';

export type CommandStatus = 'pending' | 'ack' | 'failed';

export interface CommandDoc {
  kind: CommandKind;
  issuedBy: string;                // uid
  issuedAt: Timestamp;
  payload: Record<string, unknown>;
  status: CommandStatus;
  ackAt?: Timestamp;
  error?: string;
}

// ────────────────────────────────────────────────────────────────────────────
// Config documents — live under units/{unitId}/config/{name}.
// Mutated only via *.update commands; never written directly by the client.
// ────────────────────────────────────────────────────────────────────────────

// units/{unitId}/config/relays — per-channel labels and modes for the
// Waveshare RPi Relay Board (B), 3 channels. Channel 1 doubles as the
// "security light" — its mode is mirrored in config/light below.
export interface RelayChannelConfig {
  label: string;                   // user-facing label (e.g. "Security Light")
  // auto = automatic control: sentry-driven on the light channel; engine-follow
  // (energized while the engine is running/charging) on aux channels.
  mode: 'off' | 'on' | 'auto';
}

export interface RelaysConfig {
  channels: Record<'1' | '2' | '3', RelayChannelConfig>;
}

// units/{unitId}/config/light — security light specifics.
// Channel binding lives here so the light's "owning" relay can move.
export interface LightConfig {
  relayChannel: 1 | 2 | 3;         // which Waveshare channel drives the light
  mode: 'off' | 'on' | 'auto';     // mirrored from RelaysConfig.channels[channel]
  autoTimeoutSec: number;          // seconds to stay on after last motion
  autoOnlyAfterDark: boolean;      // gate auto-on by sun-down time
}

// units/{unitId}/config/notifications — notification recipients. The events
// Cloud Function reads telegramChatIds and messages each on notifiable events
// (engine.start/stop, etc.). The bot token is a Function secret, not stored here.
export interface NotificationsConfig {
  telegramChatIds: string[];       // Telegram chat IDs to message
}

// units/{unitId}/config/sentry — motion detection + alerting.
export interface SentryConfig {
  enabled: boolean;
  sensitivity: number;             // 0–1, OpenCV bg-subtraction threshold
  recordPreSec: number;            // pre-roll seconds in saved clip
  recordPostSec: number;           // post-roll seconds in saved clip
  notifyOnMotion: boolean;         // push notification on motion events
  autoLight: boolean;              // pulse light relay on motion
  // Scare mode: when motion fires, light pulses AND the two-stroke engine
  // briefly cranks on. Bypasses quiet hours by design — it's intentional
  // alarm output, not background charging. A 5-min Pi-side cooldown
  // prevents the engine from constantly cycling on repeated triggers.
  scareMode: boolean;
  scareDurationS: number;          // how long the engine runs on a scare (sec)
}

// units/{unitId}/config/camera — live-stream parameters.
export interface CameraConfig {
  device: string;                  // v4l2 device, e.g. /dev/video0
  resolution: '480p' | '720p' | '1080p';
  fps: 15 | 24 | 30;
  // Cloudflare Stream live input — created out-of-band, stored here.
  cloudflareInputId?: string;
}

// units/{unitId}/config/charge — engine recharge scheduler config.
// Drives pi/scheduler.py; SoC thresholds defended by both BMS and scheduler.
export interface ChargeWindow {
  start: string;                   // 'HH:MM' (24h, regionTimezone-local)
  end: string;                     // 'HH:MM'
  weekdays: number[];              // 0–6, 0 = Sunday (JS convention)
}

export interface ChargeConfig {
  enabled: boolean;
  preset: 'daytime_only' | 'eco' | 'quiet_off' | 'storm' | 'custom';
  windows: ChargeWindow[];
  // socStart/socStop/socCritical are kept on the doc for legacy presets
  // but the autonomous supervisor evaluates pack voltage from the VESC,
  // not coulomb-counted SoC. The Schedule screen displays voltage from
  // config/engine.supervisor + config/engine.charge instead — see
  // EngineConfig below.
  socStart: number;                // legacy — supervisor uses voltageStart
  socStop: number;                 // legacy — supervisor uses voltageStop
  socCritical: number;             // legacy — supervisor uses voltageCritical
  quietHours: { start: string; end: string };
  allowQuietOverride: boolean;     // if true, critical SoC overrides quiet hrs
}

// units/{unitId}/config/engine — start/crank/charge/stop/supervisor knobs.
// Source of truth for the autonomous loop's voltage thresholds. The Pi
// reads sub-blocks (engine.supervisor.voltageStart, engine.charge.voltageStop)
// directly; the app needs to render them so users see the real values, not
// the legacy SoC numbers on ChargeConfig.
export interface EngineSupervisorConfig {
  enabled: boolean;                // master gate (config/charge.enabled wins if true)

  // Primary decision signal: Predator LCD coulomb-counted SoC (0–100 %).
  // Supervisor pre-wakes the LCD every tick so battery_soc is fresh.
  socStart: number;                // ≤ → auto-start in active window
  socCritical: number;             // ≤ → auto-start even in quiet hrs
  socStop: number;                 // ≥ → auto-stop (subject to load guard)

  // Load-aware proactive start. Uses Predator BMS time_to_empty_minutes
  // (runway). Fires before SoC hits socStart, but only if pack is at
  // least somewhat depleted — prevents a brief load spike at high SoC
  // from kicking the engine.
  runwayThresholdMin: number;
  proactiveSocCeiling: number;

  // Load-aware stop guard. Don't auto-stop if the BMS runway under the
  // current load is too short — we'd just restart immediately.
  sustainableRunwayMin: number;

  // Voltage fallback (consulted only when LCD is asleep AND battery_soc
  // is null in the snapshot). Curve mirrors src/lib/voltageSoc.ts.
  voltageStart: number;
  voltageCritical: number;

  tickIntervalSec: number;
  actionCooldownSec: number;
  respectQuietHours: boolean;
}

export interface EngineChargeConfig {
  currentAmps: number;             // regen draw target (amps into pack)
  voltageStop: number;             // pack volts; exit charge loop above this
  voltageMinAbort: number;         // pack volts; engine not generating, abort
  maxDurationSec: number;
  refreshHz: number;
  minRpmForLoad: number;
  maxFetTempC: number;
  maxMotorTempC: number;
  rampUpSec: number;
}

export interface EngineStartConfig {
  sparkRelayChannel: number;       // Waveshare channel that drives the spark
}

export interface EngineConfig {
  supervisor?: Partial<EngineSupervisorConfig>;
  charge?: Partial<EngineChargeConfig>;
  // Waveshare channel wired to the cooling fans. Hardwired to engine-follow on
  // the Pi (always energized while the engine runs) and hidden from the
  // Outlets screen — not user-controllable. Defaults to 3.
  fanRelayChannel?: number;
  // The app reads start.sparkRelayChannel so the Outlets screen can hide the
  // engine-follow 'auto' mode on the spark channel (it's engine-managed).
  start?: Partial<EngineStartConfig>;
  // crank/stop sub-blocks also live on this doc but the app doesn't surface
  // them yet — they're tuning knobs for the bench-test workflow.
}

// ────────────────────────────────────────────────────────────────────────────
// Current-state mirrors — live under units/{unitId}/current/{name}.
// Pi writes; client reads via onSnapshot. Never written by the client.
// ────────────────────────────────────────────────────────────────────────────

export interface LightState {
  state: boolean;                  // physical on/off right now
  mode: 'off' | 'on' | 'auto';     // effective mode
  physicalOverride: boolean;       // SPST hardware switch is forcing on
  lastChangedAt: Timestamp;
  lastChangedBy: 'app' | 'sentry' | 'physical' | 'system';
}

// units/{unitId}/current/relays — live physical state of all three relay
// channels, mirrored by the Pi. Used to show the real energized state for
// channels in 'auto' mode, where config mode alone doesn't reflect reality.
export interface RelaysState {
  channels: Record<'1' | '2' | '3', { state: boolean }>;
  lastChangedAt: Timestamp;
  lastChangedBy: 'app' | 'physical' | 'sentry' | 'engine';
}

export interface SentryState {
  armed: boolean;
  lastMotionAt: Timestamp | null;
  lastClipPath: string | null;     // Firebase Storage path of latest clip
}

// Live engine state at units/{u}/current/engine. The doc is written by
// two sources that share it via Firestore merge=true:
//   - Phase G state machine in pi/engine.py — owns `state`, `lastChangedAt`,
//     and the per-state metadata (start/peak/end fields).
//   - Phase C/G.5 scheduler (engine_supervisor) — owns `desired`, `reason`,
//     `wouldRunAt`, `lastEvalAt`. Used by ScheduleScreen.
// Consumers read whichever fields they care about and treat the other
// group as optional. All fields are optional because the initial doc on
// listener boot is just {state: 'idle', bootedAt: ...}.
export type EngineMachineState =
  | 'idle'
  | 'starting'
  | 'cranking'
  | 'running'
  | 'charging'
  | 'stopping'
  | 'failed_no_load'
  | 'failed_did_not_catch'
  | 'failed_timeout'
  | 'failed_overtemp'
  | 'failed_engine_bogged'
  | 'failed_low_voltage'
  | 'failed_error';

export interface EngineState {
  // ── Phase G state machine (engine.py) ─────────────────────────────────
  state?: EngineMachineState;
  lastChangedAt?: Timestamp;
  startedAt?: Timestamp;
  // Populated during cranking / starting / charging — the amps the loop
  // is commanding the VESC to draw. Used by the dashboard's charge tile
  // alongside the live motor_amps_in / motor_volts telemetry.
  currentAmpsCommanded?: number;
  voltageStop?: number;
  maxDurationSec?: number;
  phase?: string;                  // intra-state phase tag (e.g. 'spark_on')
  error?: string;                  // populated on failed_error
  // End-of-run metadata attached when the loop closes out. Engine.py
  // writes these on the transition into running / failed_*.
  peakCurrentAmps?: number;
  durationSec?: number;
  peakVoltage?: number;
  finalVoltage?: number;
  fetTempC?: number;
  motorTempC?: number;
  // ── Scheduler-set (engine_supervisor) ─────────────────────────────────
  desired?: 'run' | 'idle';
  reason?:
    | 'soc_low'                    // SoC under socStart inside an allowed window
    | 'soc_satisfied'              // SoC above socStop, stopping
    | 'window_closed'              // outside any charge window
    | 'quiet_hours'                // inside quiet hours and not overridden
    | 'disabled'                   // master switch off
    | 'manual_override';           // user-issued engine.override command
  wouldRunAt?: Timestamp | null;   // next time the scheduler expects to fire
  lastEvalAt?: Timestamp;
}

export interface CameraState {
  streaming: boolean;
  hlsUrl: string | null;           // populated when streaming === true
  startedAt: Timestamp | null;
  startedBy: string | null;        // uid that requested the stream
}

// ────────────────────────────────────────────────────────────────────────────
// Event log — append-only under units/{unitId}/events/{evtId}.
// Cloud Function on-create dispatches push notifications based on kind.
// ────────────────────────────────────────────────────────────────────────────

export type EventKind =
  | 'motion'
  | 'soc.critical'
  | 'engine.start'
  | 'engine.stop'
  | 'light.toggled'
  | 'sentry.armed'
  | 'sentry.disarmed'
  | 'theft.tripped'
  | 'system.online'
  | 'system.offline'
  // Fuel dashboard. `fuel.refuel` is owner-authored (source: 'app') — the one
  // event kind the client is allowed to append (see firestore.rules). The
  // `fuel.low` / `fuel.empty` alerts are emitted server-side (Pi/cloud) so
  // they can ride the existing pushFanout notification path.
  | 'fuel.refuel'
  | 'fuel.low'
  | 'fuel.empty';

export interface EventDoc {
  kind: EventKind;
  at: Timestamp;
  source: 'pi' | 'app' | 'cloud';
  payload?: Record<string, unknown>;
  // Optional rich attachments — populated for motion events with a clip.
  clipPath?: string;
  thumbnailPath?: string;
}

// engine.stop payload enrichment — see pi/fuel_engine_stop_enrichment.md.
// All fields optional: older Pi firmware emits a bare {state} payload, so the
// fuel model must degrade gracefully when reason/voltageAtStop/socAtStop are
// absent (falls back to full→full calibration; no dry-stop detection).
export type EngineStopReason = 'supervisor_full' | 'manual' | 'stalled' | 'fault';

export interface EngineStopPayload {
  state?: string;
  reason?: EngineStopReason;       // 'stalled' + battery-not-full ⇒ ran dry
  voltageAtStop?: number;          // pack volts at the transition to idle
  socAtStop?: number | null;       // LCD SoC at stop, null if unmapped
}

export type FuelRefuelMethod = 'full' | 'add';

// Payload for a `fuel.refuel` event (written by the app, source: 'app').
//   method 'full' → tank filled to capacity; `gallons` is the OPTIONAL
//                   "how much did it take?" amount that, when present, feeds
//                   burn-rate calibration.
//   method 'add'  → partial top-off; `gallons` is required.
// `levelAfter` is the app-computed tank level (gallons) right after the fill,
// persisted so the model can anchor without replaying the whole refuel chain.
export interface FuelRefuelPayload {
  method: FuelRefuelMethod;
  gallons: number | null;
  levelAfter: number;
}

// units/{unitId}/fuel/settings — owner-writable fuel dashboard settings.
// Unlike config/* docs (Pi-owned, client-read-only), this lives under a
// dedicated `fuel` subcollection the security rules let the owner write:
// tank size / price / burn-rate prior are user preferences with no hardware
// side effects.
export interface FuelSettings {
  tankGallons: number;             // usable capacity of the onboard tank (gal)
  pricePerGallon?: number;         // optional — enables cost readouts
  // Seed prior for gal/hr, used until the model has ≥1 calibration sample.
  seedBurnRateGalPerHour: number;
}

// ────────────────────────────────────────────────────────────────────────────
// Push notification tokens — users/{uid}/pushTokens/{token}.
// One doc per device. Cloud Function reads these to fan out FCM/Expo Push.
// ────────────────────────────────────────────────────────────────────────────

export interface PushTokenDoc {
  token: string;                   // Expo push token (ExponentPushToken[...])
  platform: 'ios' | 'android' | 'web';
  deviceName?: string;
  createdAt: Timestamp;
  lastSeenAt: Timestamp;
}
