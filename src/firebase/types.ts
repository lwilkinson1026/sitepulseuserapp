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
export type CommandKind =
  | 'outlet.toggle'
  | 'theft.arm'
  | 'theft.disarm'
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
