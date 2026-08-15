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
    SITEPULSE_INTERVAL             publish interval seconds, default 15
    SITEPULSE_LASTSEEN_INTERVAL    unit-doc heartbeat interval seconds, default 300
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

import pi_health
from predator_decoder import FrameAssembler, decode_frame
from predator_i2c_sniffer import PassiveI2cSniffer
from vesc_listener import VescListener


UNIT_ID            = os.environ.get("SITEPULSE_UNIT_ID", "UNIT-001")
SERVICE_ACCOUNT    = os.path.expanduser(
    os.environ.get("SITEPULSE_SA", "~/sitepulse/service-account.json")
)
# Firestore bills — and the Spark plan hard-caps — per document write, not
# per byte. At the original 3 s this loop wrote 2 documents every cycle:
# 57,600 writes/day/unit against a 20,000/day free-tier ceiling. UNIT-002
# exhausted the daily quota by 20:20 PT on 2026-08-14 and every write after
# that returned `429 Quota exceeded`, which surfaces as the app silently
# going stale while the Pi itself looks perfectly healthy.
#
# 15 s is a deliberate product call, not just a cost one: a generator's SoC,
# output watts and pack voltage do not carry 3-second-resolution information.
# Lower it temporarily (SITEPULSE_INTERVAL=3) when watching a crank sequence
# live, but do not ship it that way.
PUBLISH_INTERVAL_S = float(os.environ.get("SITEPULSE_INTERVAL", "15"))

# The unit doc's `lastSeen` is a liveness heartbeat, not telemetry — and the
# snapshot one level down already carries `last_update` stamped from the same
# server clock. Writing both on every cycle doubled write volume to convey
# nothing new. Decoupled here so the heartbeat stays coarse while telemetry
# cadence can be tuned independently.
#
# 300 s is deliberately coarse: grepping src/ and functions/src/, `lastSeen`
# appears only in a type declaration — nothing reads it. The app classifies
# staleness from the snapshot's `last_update`. Tighten this only when
# something actually consumes it.
LASTSEEN_INTERVAL_S = float(os.environ.get("SITEPULSE_LASTSEEN_INTERVAL", "300"))

WATCH_ADDRESS      = int(os.environ.get("SITEPULSE_PREDATOR_ADDR", "0x3E"), 0)
PUBLISH_RAW        = os.environ.get("SITEPULSE_PREDATOR_PUBLISH_RAW") == "1"

# How long a decoded LCD frame stays credible. Past this, the Predator's
# panel is presumed dark and every LCD-derived field publishes as null.
#
# This exists because the panel does not announce that it went to sleep —
# the BMS↔LCD bus simply goes quiet. Without a staleness check the last
# frame ever seen gets re-decoded and republished forever, so a pack that
# was at 90 % when the display slept keeps reporting 90 % indefinitely.
# That reads as a healthy battery, which stops the charge scheduler cold;
# observed on UNIT-002 as `soc=84% ... frames=0 rate=0.0Hz` republished on
# every cycle for as long as the panel stayed dark.
#
# Must comfortably exceed the frame period. The decoder runs ~3-6 Hz when
# awake, so even a heavily CPU-starved unit lands a frame every few
# hundred ms; 15 s is ~50x margin and still detects sleep within one
# supervisor tick.
LCD_STALE_S        = float(os.environ.get("SITEPULSE_LCD_STALE_S", "15"))

# Fields that come from the LCD and are therefore meaningless once it goes
# dark. Nulled together — publishing SoC without watts (or vice versa)
# would imply one is fresher than the other.
_LCD_DERIVED_FIELDS = (
    "battery_soc",
    "output_watts",
    "time_to_empty_minutes",
    "time_to_full_minutes",
)
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
        # Monotonic stamp of the last COMPLETED frame. None until the first
        # one lands. Deliberately not reset by reset() — it tracks the bus,
        # not the publish window.
        self.last_frame_at: Optional[float] = None
        self.lcd_awake: bool = False        # last computed staleness verdict
        self.warning_log_seen: set[str] = set()  # rate-limit duplicate warning lines

    def reset(self) -> None:
        self.frames_decoded = 0
        self.frames_window_start = time.monotonic()

    def lcd_is_fresh(self, now: float) -> bool:
        """True when a frame arrived recently enough to trust the decode."""
        if self.last_frame_at is None:
            return False
        return (now - self.last_frame_at) <= LCD_STALE_S


stats = _Stats()

# Module-level so build_snapshot() can read it without main() passing it in.
# Set in main() right after the listener starts.
_vesc: Optional[VescListener] = None


def build_snapshot() -> Optional[dict]:
    """Build the dict written to units/{UNIT_ID}/current/snapshot.

    Returns None only when there is genuinely nothing to say — no Predator
    frame AND no VESC telemetry.

    This used to bail out whenever the Predator had never produced a frame,
    which coupled the ENTIRE snapshot to the state of one peripheral: with
    the Predator powered off, pack voltage, RPM and current from the VESC
    never reached Firestore either, and the app showed a unit that looked
    dead while the CAN bus was happily reporting 48.5 V. Observed on
    UNIT-002 2026-08-04. The two data sources are independent and must fail
    independently.
    """
    have_frame = stats.last_raw_frame is not None
    have_vesc = _vesc is not None

    if not have_frame and not have_vesc:
        return None

    decoded = decode_frame(stats.last_raw_frame) if have_frame else None

    # Frame rate: completed frames over the publish window.
    elapsed = max(0.001, time.monotonic() - stats.frames_window_start)
    frame_rate_hz = round(stats.frames_decoded / elapsed, 2)

    if decoded is None:
        # Never saw a frame this process's lifetime. Publish the LCD fields
        # as explicitly absent rather than omitting them, so a consumer can
        # tell "Predator is dark" from "this Pi runs an old publisher".
        snap: dict = {
            "battery_soc":           None,
            "dc_active":             False,
            "ac_active":             False,
            "output_mode":           "unknown",
            "output_watts":          None,
            "time_to_empty_minutes": None,
            "time_to_full_minutes":  None,
            "charging":              False,
            "system_mode":           "unknown",
            "lcd_frame_rate_hz":     frame_rate_hz,
            "lcd_awake":             False,
            "bmsFaults":             [],
        }
        if _vesc is not None:
            for k, v in _vesc.snapshot().items():
                snap[k] = v
        stats.lcd_awake = False
        stats.last_warnings = []
        return snap

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

    # ── Staleness gate ────────────────────────────────────────────────────
    # decode_frame() above ran against last_raw_frame, which is whatever
    # arrived most recently — possibly minutes ago. The decoder cannot tell
    # the difference; a stale frame decodes into a perfectly well-formed,
    # perfectly wrong reading. Only the arrival time can, so the check
    # belongs here rather than in the decoder.
    #
    # Note this is NOT the same condition as the decoder's blank-field
    # check. A sleeping panel that still clocks blank frames decodes to
    # None on its own; a panel switched fully dark stops transmitting
    # entirely, and that is the case this catches.
    fresh = stats.lcd_is_fresh(time.monotonic())
    stats.lcd_awake = fresh
    snap["lcd_awake"] = fresh
    if not fresh:
        for key in _LCD_DERIVED_FIELDS:
            snap[key] = None
        # output_mode/system_mode describe the LCD's own display state and
        # are strings, so they get an explicit "unknown" rather than null —
        # the app switches on them and a null would render as "off", which
        # is a claim we cannot make about a panel we can't see.
        snap["output_mode"] = "unknown"
        snap["system_mode"] = "unknown"

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


# monotonic stamp of the last unit-doc heartbeat; 0.0 forces one on the
# first publish so a freshly booted unit marks itself live immediately.
_last_seen_written = 0.0


def publish(db: firestore.Client, snap: dict) -> None:
    global _last_seen_written

    snap["last_update"] = firestore.SERVER_TIMESTAMP
    db.document(f"units/{UNIT_ID}/current/snapshot").set(snap)

    # Heartbeat on its own slower clock — see LASTSEEN_INTERVAL_S. Skipping
    # it costs nothing: `last_update` on the snapshot above is written every
    # cycle from the same server clock, so liveness is still derivable at
    # full telemetry resolution.
    now = time.monotonic()
    if now - _last_seen_written >= LASTSEEN_INTERVAL_S:
        db.document(f"units/{UNIT_ID}").set(
            {"lastSeen": firestore.SERVER_TIMESTAMP},
            merge=True,
        )
        _last_seen_written = now


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
    _overheat = pi_health.OverheatWatcher(db, UNIT_ID)

    # Wake at least once a second regardless of publish cadence. When the
    # panel is dark no transactions arrive, so this timeout is the only thing
    # driving the loop — if it equalled PUBLISH_INTERVAL_S, ordinary jitter
    # would push the publish gate past its deadline and land on the *next*
    # wakeup, silently halving the effective publish rate.
    _tick_s = min(PUBLISH_INTERVAL_S, 1.0)

    with PassiveI2cSniffer() as sniff:
        for txn in sniff.transactions(timeout_s=_tick_s):
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
                        stats.last_frame_at = now
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
                if snap is not None:
                    health = pi_health.snapshot()
                    # Host health is merged here rather than inside
                    # build_snapshot() so it lands on BOTH the normal path and
                    # the LCD-dark path — the Pi's own temperature is the one
                    # reading that stays meaningful when every peripheral has
                    # gone quiet, which is exactly when you want it.
                    snap.update(health)
                    # Watch for a SUSTAINED crossing and emit an event the
                    # notifier can act on. Publishing the temperature only
                    # helps someone who is already looking; a unit in the
                    # field has nobody looking.
                    if _overheat is not None:
                        _overheat.observe(health)
                if snap is None:
                    # Neither Predator nor VESC has anything — genuinely
                    # nothing to publish.
                    if now - last_published > 10:
                        print("[telemetry] no Predator frames and no VESC "
                              "listener — nothing to publish", flush=True)
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
                if not snap.get("lcd_awake", True):
                    # Say so plainly. The old log line printed a confident
                    # `soc=84%` while `frames=0 rate=0.0Hz` sat right beside
                    # it — all the evidence was on screen and still read as
                    # a working unit.
                    flow = "LCD DARK (all LCD-derived fields null)"
                elif snap["system_mode"] == "charging":
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
