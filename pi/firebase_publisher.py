"""
SitePulse Firebase publisher.

Sniffs the Predator 2000W's BMS↔LCD I²C bus (passive parallel tap on the
LCD connector), decodes the LCD frame, and publishes a TelemetrySnapshot
to Firestore at units/{UNIT_ID}/current/snapshot every PUBLISH_INTERVAL
seconds.

Pi wiring (parallel with the LCD harness):
    GPIO 22 (pin 15) → SDA   (LCD white wire)
    GPIO 23 (pin 16) → SCL   (LCD yellow wire)
    GND     (pin 6)  → GND   (LCD green wire)
    DO NOT connect any Pi power rail to the Predator.

Note: deliberately NOT GPIO 2/3 — those are the kernel's i2c-1 bus where
the PCA9685 servo driver lives.  We bit-bang via lgpio so any GPIO pair
works; 22/23 are adjacent on the header and unused elsewhere in the stack.

Setup on the Pi:
    pip3 install --break-system-packages firebase-admin lgpio

    chmod 600 ~/sitepulse/service-account.json
    SITEPULSE_SA=~/sitepulse/service-account.json python3 firebase_publisher.py

Env vars:
    SITEPULSE_UNIT_ID              default UNIT-001
    SITEPULSE_SA                   path to service account JSON
    SITEPULSE_INTERVAL             publish interval seconds, default 3
    SITEPULSE_PREDATOR_ADDR        I²C address to watch, default 0x3E
    SITEPULSE_PREDATOR_SDA_PIN     BCM GPIO for SDA, default 22
    SITEPULSE_PREDATOR_SCL_PIN     BCM GPIO for SCL, default 23
    SITEPULSE_PREDATOR_GPIO_CHIP   lgpio chip id, default 0
    SITEPULSE_PREDATOR_PUBLISH_RAW "1" to include raw_frame_hex in published docs
    SITEPULSE_DEBUG                "1" to log every decoded frame, not just publishes
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import firebase_admin
from firebase_admin import credentials, firestore

from predator_decoder import FrameAssembler, decode_frame
from predator_i2c_sniffer import PassiveI2cSniffer
from vesc_listener import VescListener


UNIT_ID            = os.environ.get("SITEPULSE_UNIT_ID", "UNIT-001")
SERVICE_ACCOUNT    = os.path.expanduser(
    os.environ.get("SITEPULSE_SA", "~/sitepulse/service-account.json")
)
PUBLISH_INTERVAL_S = float(os.environ.get("SITEPULSE_INTERVAL", "3"))
WATCH_ADDRESS      = int(os.environ.get("SITEPULSE_PREDATOR_ADDR", "0x3E"), 0)
PUBLISH_RAW        = os.environ.get("SITEPULSE_PREDATOR_PUBLISH_RAW") == "1"
DEBUG              = os.environ.get("SITEPULSE_DEBUG") == "1"
# VESC integration is opt-out so the publisher still runs cleanly on a
# bench Pi without the CAN HAT seated. Set to "0" to disable.
VESC_ENABLED       = os.environ.get("SITEPULSE_VESC_ENABLED", "1") != "0"


# Diagnostic counters reset every publish cycle.
class _Stats:
    def __init__(self) -> None:
        self.frames_decoded = 0
        self.frames_window_start = time.monotonic()
        self.last_warnings: list[str] = []
        self.last_raw_frame: Optional[list[int]] = None
        self.warning_log_seen: set[str] = set()  # rate-limit duplicate warning lines

    def reset(self) -> None:
        self.frames_decoded = 0
        self.frames_window_start = time.monotonic()


stats = _Stats()

# Module-level so build_snapshot() can read it without main() passing it in.
# Set in main() right after the listener starts.
_vesc: Optional[VescListener] = None


def build_snapshot() -> Optional[dict]:
    """Build the dict written to units/{UNIT_ID}/current/snapshot.

    Returns None if no frame has been decoded yet (e.g., the BMS hasn't
    started talking, or we're still waiting for the first full sweep).
    """
    if stats.last_raw_frame is None:
        return None

    decoded = decode_frame(stats.last_raw_frame)

    # Frame rate: completed frames over the publish window.
    elapsed = max(0.001, time.monotonic() - stats.frames_window_start)
    frame_rate_hz = round(stats.frames_decoded / elapsed, 2)

    snap: dict = {
        "battery_soc":           decoded["battery_soc"],
        "dc_active":             decoded["dc_active"],
        "ac_active":             decoded["ac_active"],
        "output_mode":           decoded["output_mode"],
        "output_watts":          decoded["output_watts"],
        "time_to_empty_minutes": decoded["time_to_empty_minutes"],
        "time_to_full_minutes":  decoded["time_to_full_minutes"],
        "charging":              decoded["charging"],
        "system_mode":           decoded["system_mode"],
        "lcd_frame_rate_hz":     frame_rate_hz,
        # Preserve the array shape the existing app expects.  Empty until
        # we map LCD fault icons (low battery, overload, overheat, etc.).
        "bmsFaults":             [],
    }

    if PUBLISH_RAW:
        snap["raw_frame_hex"] = " ".join(f"{b:02X}" for b in stats.last_raw_frame)

    # Merge in the latest VESC motor-controller telemetry. The listener
    # maintains its own thread-safe latest-value cache; we just pull a
    # snapshot at publish cadence. Predator and VESC never overlap on
    # keys — Predator owns battery_soc + AC/DC state, VESC owns motor_*.
    if _vesc is not None:
        for k, v in _vesc.snapshot().items():
            snap[k] = v

    # Stash warnings so main() can log them.
    stats.last_warnings = list(decoded["warnings"])
    return snap


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


def _log_warnings_once(warnings: list[str]) -> None:
    """De-dupe identical warning strings across publishes — a 78% SoC byte
    we've already seen unmapped shouldn't spam the log on every cycle."""
    for w in warnings:
        if w in stats.warning_log_seen:
            continue
        stats.warning_log_seen.add(w)
        print(f"[predator] {w}", file=sys.stderr, flush=True)


def main() -> None:
    global _vesc

    db = init_firestore()
    print(
        f"[sitepulse] Predator I²C sniffer starting "
        f"(addr=0x{WATCH_ADDRESS:02X}, publishing {UNIT_ID} every {PUBLISH_INTERVAL_S}s)",
        flush=True,
    )

    if VESC_ENABLED:
        _vesc = VescListener()
        _vesc.start()
        # Don't block on it — if the CAN bus is down or python-can is missing
        # the listener prints a one-line warning and exits its thread; the
        # publisher continues with Predator data only.

    assembler = FrameAssembler()
    last_published = 0.0

    with PassiveI2cSniffer() as sniff:
        for txn in sniff.transactions(timeout_s=PUBLISH_INTERVAL_S):
            now = time.monotonic()

            # Process whatever transactions arrived since the last loop.
            if txn is not None and txn.address == WATCH_ADDRESS and txn.ack and len(txn.data) == 3:
                # Predator BMS writes 3-byte commands: [0x80, reg, val].
                if txn.data[0] == 0x80:
                    reg, val = txn.data[1], txn.data[2]
                    completed = assembler.feed(reg, val)
                    if completed is not None:
                        stats.frames_decoded += 1
                        stats.last_raw_frame = completed
                        if DEBUG:
                            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                            hex_str = " ".join(f"{b:02X}" for b in completed)
                            print(f"[{stamp}] frame: {hex_str}", flush=True)

            # Periodic publish — independent of inbound transactions so we
            # always heartbeat even if the bus stalls.
            if now - last_published < PUBLISH_INTERVAL_S:
                continue

            try:
                snap = build_snapshot()
                if snap is None:
                    if now - last_published > 10:
                        print("[predator] no frames decoded yet (bus quiet?)", flush=True)
                    continue
                publish(db, snap)
                last_published = now
                _log_warnings_once(stats.last_warnings)
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                motor_summary = ""
                if _vesc is not None and not snap.get("motor_stale", True):
                    motor_summary = (
                        f"  rpm={snap.get('motor_rpm', '?')}  "
                        f"V={snap.get('motor_volts', '?')}  "
                        f"A={snap.get('motor_amps', '?')}"
                    )
                # While charging the output row is dark, so mode/watts/ttempty
                # are all None by design; showing the charge ETA instead keeps
                # the log line informative rather than a row of blanks.
                if snap["system_mode"] == "charging":
                    flow = f"CHARGING  ttfull={snap['time_to_full_minutes']}m"
                else:
                    flow = (
                        f"mode={snap['output_mode']}  "
                        f"watts={snap['output_watts']}  "
                        f"ttempty={snap['time_to_empty_minutes']}m"
                    )
                print(
                    f"[{stamp}] {UNIT_ID}  "
                    f"soc={snap['battery_soc']}%  "
                    f"{flow}  "
                    f"frames={stats.frames_decoded}  rate={snap['lcd_frame_rate_hz']}Hz"
                    f"{motor_summary}",
                    flush=True,
                )
                stats.reset()
            except Exception as e:
                print(f"[firestore] write failed: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
