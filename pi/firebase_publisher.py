"""
SitePulse Firebase publisher.

Reads VESC BMS CAN frames directly from `can0` (no TCP intermediary) and
publishes a TelemetrySnapshot to Firestore at units/{UNIT_ID}/current/snapshot
every PUBLISH_INTERVAL seconds.

Decodes the standard VESC BMS packet types:
  0x26 V_TOT       pack voltage, charge-port voltage
  0x27 I           pack current (signed: + = charge, - = discharge)
  0x29 V_CELL      per-cell voltages (3 cells per frame, indexed)
  0x2B TEMPS       temperatures (3 per frame, indexed)
  0x2D SOC_SOH_T   state of charge, state of health, IC temp, status

Arbitration ID layout (VESC convention):
    (packet_type << 8) | vesc_id

Setup on the Pi:
    pip3 install --break-system-packages firebase-admin python-can
    # python-can is usually already installed (bms_tcp_server.py uses it)

    chmod 600 ~/sitepulse/service-account.json
    SITEPULSE_SA=~/sitepulse/service-account.json python3 firebase_publisher.py

Env vars:
    SITEPULSE_UNIT_ID    default UNIT-001
    SITEPULSE_SA         path to service account JSON
    SITEPULSE_CAN        CAN interface, default can0
    SITEPULSE_VESC_ID    VESC unit id on the bus, default 3
    SITEPULSE_INTERVAL   publish interval seconds, default 3
    SITEPULSE_DEBUG      "1" to dump raw cell/temp arrays each publish
"""

import os
import struct
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import can
import firebase_admin
from firebase_admin import credentials, firestore


UNIT_ID            = os.environ.get("SITEPULSE_UNIT_ID", "UNIT-001")
SERVICE_ACCOUNT    = os.path.expanduser(
    os.environ.get("SITEPULSE_SA", "~/sitepulse/service-account.json")
)
CAN_CHANNEL        = os.environ.get("SITEPULSE_CAN", "can0")
VESC_ID            = int(os.environ.get("SITEPULSE_VESC_ID", "3"))
PUBLISH_INTERVAL_S = float(os.environ.get("SITEPULSE_INTERVAL", "3"))
DEBUG              = os.environ.get("SITEPULSE_DEBUG") == "1"


# VESC BMS CAN packet types
PKT_V_TOT             = 0x26
PKT_I                 = 0x27
PKT_AH_WH             = 0x28
PKT_V_CELL            = 0x29
PKT_BAL               = 0x2A
PKT_TEMPS             = 0x2B
PKT_HUM               = 0x2C
PKT_SOC_SOH_TEMP_STAT = 0x2D


class BMSState:
    def __init__(self):
        self.v_tot = 0.0
        self.v_charge = 0.0
        self.i_in = 0.0
        # Harmony emits zero-valued 0x27 frames as "no fresh measurement"
        # between real readings. Hold the last non-zero value until we see
        # enough consecutive zeros to call it true idle.
        self.i_in_zero_streak = 0
        self.soc = 0.0          # 0.0 - 1.0 ratio
        self.soh = 0.0
        self.t_ic = 0.0
        self.is_charging = False
        self.is_balancing = False
        self.cells: list[float] = []
        self.n_cells = 0
        self.temps: list[float] = []
        self.n_temps = 0
        self.last_frame_ts = 0.0


state = BMSState()


def _u16_be(d, off): return (d[off] << 8) | d[off + 1]
def _i16_be(d, off):
    v = _u16_be(d, off)
    return v - 0x10000 if v & 0x8000 else v
def _f32_be(d, off):
    return struct.unpack(">f", bytes(d[off:off + 4]))[0]


def decode_frame(arb_id: int, data: bytes) -> None:
    pkt_type = (arb_id >> 8) & 0xFF
    vesc_id  = arb_id & 0xFF
    if vesc_id != VESC_ID:
        return

    state.last_frame_ts = time.monotonic()

    if pkt_type == PKT_V_TOT and len(data) >= 8:
        state.v_tot    = _f32_be(data, 0)
        state.v_charge = _f32_be(data, 4)

    elif pkt_type == PKT_I and len(data) >= 8:
        # Harmony 16 leaves bytes 0-3 (i_in) at zero and reports magnitude
        # only in bytes 4-7 (i_in_ic). No direction bit yet, so assume
        # discharge (negative) by default — flip once we wire charger detection.
        i_in_ic = _f32_be(data, 4)
        if abs(i_in_ic) >= 0.5:
            state.i_in = -i_in_ic
            state.i_in_zero_streak = 0
        else:
            state.i_in_zero_streak += 1
            # After ~10 consecutive zero frames, accept true idle.
            if state.i_in_zero_streak >= 10:
                state.i_in = 0.0

    elif pkt_type == PKT_V_CELL and len(data) >= 4:
        cell_num = data[0]
        n_cells  = data[1]
        state.n_cells = n_cells
        if len(state.cells) < n_cells:
            state.cells.extend([0.0] * (n_cells - len(state.cells)))
        # up to 3 cells (f16, scale 1e3) after the 2-byte header
        for i in range((len(data) - 2) // 2):
            idx = cell_num + i
            if 0 <= idx < n_cells:
                state.cells[idx] = _u16_be(data, 2 + i * 2) / 1000.0

    elif pkt_type == PKT_TEMPS and len(data) >= 4:
        adc_num = data[0]
        n_temps = data[1]
        state.n_temps = n_temps
        if len(state.temps) < n_temps:
            state.temps.extend([0.0] * (n_temps - len(state.temps)))
        # up to 3 temps (f16 signed, scale 1e2) after the 2-byte header
        for i in range((len(data) - 2) // 2):
            idx = adc_num + i
            if 0 <= idx < n_temps:
                state.temps[idx] = _i16_be(data, 2 + i * 2) / 100.0

    elif pkt_type == PKT_SOC_SOH_TEMP_STAT and len(data) >= 7:
        # Harmony 16 firmware packs SoC as a single byte scaled to 0-255,
        # and IC temp as a signed byte at offset 6. This differs from upstream
        # VESC BMS firmware which uses float16 fields.
        state.soc  = data[4] / 255.0
        state.t_ic = struct.unpack_from("b", bytes(data), 6)[0]


def build_snapshot() -> dict:
    valid_cells = [c for c in state.cells if c > 0.5]
    if valid_cells:
        vmin = min(valid_cells)
        vmax = max(valid_cells)
        vdelta = vmax - vmin
    else:
        vmin = vmax = vdelta = 0.0

    valid_temps = [t for t in state.temps if -40 < t < 100]
    if valid_temps:
        tmin = min(valid_temps)
        tmax = max(valid_temps)
        tavg = sum(valid_temps) / len(valid_temps)
    else:
        tmin = tmax = tavg = state.t_ic

    if state.i_in > 0.5:
        mode = "charging"
    elif state.i_in < -0.5:
        mode = "discharging"
    else:
        mode = "idle"

    return {
        "battery_soc":     round(state.soc * 100, 1),
        "battery_voltage": round(state.v_tot, 2),
        "battery_current": round(state.i_in, 2),
        "battery_temp":    round(tavg, 1),
        "battery_power":   round(state.v_tot * state.i_in, 1),
        "cells": {
            "voltages":     [round(c, 3) for c in state.cells],
            "voltageMin":   round(vmin, 3),
            "voltageMax":   round(vmax, 3),
            "voltageDelta": round(vdelta, 3),
            "tempMin":      round(tmin, 1),
            "tempMax":      round(tmax, 1),
            "tempAvg":      round(tavg, 1),
        },
        "bmsFaults": [],
        "system_mode": mode,
    }


def init_firestore() -> firestore.Client:
    if not os.path.exists(SERVICE_ACCOUNT):
        print(
            f"[fatal] service account not found at {SERVICE_ACCOUNT}\n"
            f"  set SITEPULSE_SA env var or copy the JSON there.",
            file=sys.stderr,
        )
        sys.exit(1)
    cred = credentials.Certificate(SERVICE_ACCOUNT)
    firebase_admin.initialize_app(cred)
    return firestore.client()


def publish(db: firestore.Client, snap: dict) -> None:
    snap["last_update"] = firestore.SERVER_TIMESTAMP
    db.document(f"units/{UNIT_ID}/current/snapshot").set(snap)
    db.document(f"units/{UNIT_ID}").set(
        {"lastSeen": firestore.SERVER_TIMESTAMP},
        merge=True,
    )


def main() -> None:
    db = init_firestore()
    bus = can.interface.Bus(channel=CAN_CHANNEL, interface="socketcan")
    print(
        f"[sitepulse] CAN listener on {CAN_CHANNEL}, "
        f"vesc_id={VESC_ID}, publishing {UNIT_ID} every {PUBLISH_INTERVAL_S}s"
    )

    last_published = 0.0
    for msg in bus:
        decode_frame(msg.arbitration_id, msg.data)

        now = time.monotonic()
        if now - last_published < PUBLISH_INTERVAL_S:
            continue
        if state.last_frame_ts == 0:
            continue  # haven't seen any frames yet

        try:
            snap = build_snapshot()
            publish(db, snap)
            last_published = now
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            cells_seen = sum(1 for c in state.cells if c > 0.5)
            print(
                f"[{stamp}] {UNIT_ID}  "
                f"soc={snap['battery_soc']}%  "
                f"v={snap['battery_voltage']}V  "
                f"i={snap['battery_current']}A  "
                f"t={snap['battery_temp']}°C  "
                f"mode={snap['system_mode']}  "
                f"cells={cells_seen}/{state.n_cells}"
            )
            if DEBUG:
                print(f"  cells: {state.cells}")
                print(f"  temps: {state.temps}")
                print(f"  soc_raw={state.soc} soh_raw={state.soh} t_ic={state.t_ic}")
        except Exception as e:
            print(f"[firestore] write failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
