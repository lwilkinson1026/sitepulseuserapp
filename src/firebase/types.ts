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
// Keep the snapshot under ~2 KB by trimming cells to fixed precision.
export interface CellPack {
  voltages: number[];              // length 16, units V, 3 decimals
  voltageMin: number;
  voltageMax: number;
  voltageDelta: number;            // max - min, useful for balancing health
  tempMin: number;                 // °C
  tempMax: number;                 // °C
  tempAvg: number;                 // °C
}

// units/{unitId}/current/snapshot — Pi overwrites this every 2–5 s.
// The app listens with onSnapshot and renders the dashboard.
export interface TelemetrySnapshot {
  // Battery — sourced from VESC Harmony 16 over CAN
  battery_soc: number;             // 0–100 %
  battery_voltage: number;         // pack V
  battery_current: number;         // pack A — positive = discharge, negative = charge
  battery_temp: number;            // °C (pack average)
  battery_power: number;           // V × I, signed W
  cells?: CellPack;                // optional richer cell breakdown
  cycleCount?: number;             // BMS-reported cycles
  bmsFaults?: string[];            // active fault codes from the BMS

  // Inverter — not yet integrated (phase 2). Pi sends nulls / zeros until then.
  inverter_power?: number;         // W
  inverter_voltage?: number;       // V AC RMS
  inverter_frequency?: number;     // Hz
  inverter_load_percent?: number;  // 0–100 %

  // Outlets — relay state, 1-indexed channels (phase 1)
  outlets?: Record<string, boolean>;

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
  | 'schedule.update';

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
  mode: 'off' | 'on' | 'auto';     // auto = sentry-controlled (channel 1 only)
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
  socStart: number;                // engine starts when SoC drops below this
  socStop: number;                 // engine stops when SoC rises above this
  socCritical: number;             // emergency floor for notifications
  quietHours: { start: string; end: string };
  allowQuietOverride: boolean;     // if true, critical SoC overrides quiet hrs
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

export interface SentryState {
  armed: boolean;
  lastMotionAt: Timestamp | null;
  lastClipPath: string | null;     // Firebase Storage path of latest clip
}

export interface EngineState {
  desired: 'run' | 'idle';
  reason:
    | 'soc_low'                    // SoC under socStart inside an allowed window
    | 'soc_satisfied'              // SoC above socStop, stopping
    | 'window_closed'              // outside any charge window
    | 'quiet_hours'                // inside quiet hours and not overridden
    | 'disabled'                   // master switch off
    | 'manual_override';           // user-issued engine.override command
  wouldRunAt: Timestamp | null;    // next time the scheduler expects to fire
  lastEvalAt: Timestamp;
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
  | 'system.offline';

export interface EventDoc {
  kind: EventKind;
  at: Timestamp;
  source: 'pi' | 'app' | 'cloud';
  payload?: Record<string, unknown>;
  // Optional rich attachments — populated for motion events with a clip.
  clipPath?: string;
  thumbnailPath?: string;
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
