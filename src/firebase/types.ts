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
