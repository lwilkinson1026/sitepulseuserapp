"""
Seed default config docs at units/{UNIT_ID}/config/* on first boot.

Idempotent: each doc is written with `merge=True` only when it doesn't
exist yet, so re-running this never clobbers user edits.

Usage:
    SITEPULSE_SA=~/sitepulse/service-account.json python3 seed_config.py

Defaults match the locked-in decisions documented in the feature plan:
  - Waveshare RPi Relay Board (B), 3 channels; channel 1 = security light
  - LiFePO4 15S thresholds: socStart 25, socStop 85, socCritical 12
  - Quiet hours 21:00–07:00, allowQuietOverride = false
  - Camera: /dev/video0, 720p @ 24fps, no Cloudflare input bound yet
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict

import firebase_admin
from firebase_admin import credentials, firestore


UNIT_ID         = os.environ.get("SITEPULSE_UNIT_ID", "UNIT-001")
SERVICE_ACCOUNT = os.path.expanduser(
    os.environ.get("SITEPULSE_SA", "~/sitepulse/service-account.json")
)


DEFAULTS: Dict[str, Dict[str, Any]] = {
    "relays": {
        "channels": {
            "1": {"label": "Security Light",   "mode": "auto"},
            "2": {"label": "Aux 2",            "mode": "off"},
            # Channel 3 follows the engine by default: 'auto' energizes it
            # whenever the engine is running/charging (e.g. cooling fans).
            "3": {"label": "User Aux Output",  "mode": "auto"},
        },
    },
    "light": {
        "relayChannel": 1,
        "mode": "auto",
        "autoTimeoutSec": 90,
        "autoOnlyAfterDark": True,
    },
    "sentry": {
        "enabled": False,
        "sensitivity": 0.4,
        "recordPreSec": 5,
        "recordPostSec": 10,
        "notifyOnMotion": True,
        "autoLight": True,
        # Scare mode is OFF by default — opt-in. When on, motion fires
        # the light AND cranks the two-stroke engine for scareDurationS.
        "scareMode": False,
        "scareDurationS": 20,
    },
    "camera": {
        "device": "/dev/video0",
        "resolution": "720p",
        "fps": 24,
        # cloudflareInputId left unset until the user provisions a live input
    },
    "servos": {
        # Three servos:
        #   choke  — PCA9685 ch1, engine choke linkage (calibrate end-stops
        #            against the real linkage; pre-linkage bench seed is
        #            1333-1667 us = 60-120 deg in the 1000-2000 us mapping).
        #   button — PCA9685 ch2, micro servo arm that taps the Predator
        #            LCD wake button to defeat its 10-min sleep timer.
        #            1000-2000 us is a safe-but-loose default; calibrate
        #            with `python3 servo_test.py 2` before mounting the arm.
        #   ac     — PCA9685 ch3, micro servo arm that taps the Predator
        #            AC power toggle button. Each press flips AC state.
        #            Calibrate with `python3 servo_test.py 3`.
        "choke":  {"channel": 1, "minUs": 1333, "maxUs": 1667, "slewRate": 2.0,
                   "defaultOnStart": 0.0},
        "button": {"channel": 2, "minUs": 1000, "maxUs": 2000, "slewRate": 10.0,
                   "defaultOnStart": 0.0},
        "ac":     {"channel": 3, "minUs": 1000, "maxUs": 2000, "slewRate": 10.0,
                   "defaultOnStart": 0.0},
        "presets": {
            "idle":       {"choke": 0.0},
            "start_cold": {"choke": 1.0},
            "start_warm": {"choke": 0.3},
            "run":        {"choke": 0.0},
            "press":      {"button": 1.0},
            "release":    {"button": 0.0},
            "ac_press":   {"ac": 1.0},
            "ac_release": {"ac": 0.0},
        },
    },
    "lcdWake": {
        # Periodic LCD wake-button presser. Opt-in: stays off until the
        # user calibrates the button servo and explicitly enables it
        # (either from the app or by setting enabled=true in Firestore).
        # 240s (4 min) keeps the LCD continuously awake — the Predator's
        # display sleeps after ~10 min idle, so 240s gives ~2.5x safety
        # margin. The supervisor also pre-wakes the LCD at every tick
        # before reading SoC, so this is now the secondary safety net.
        "enabled": False,
        "intervalSec": 240,
        "pressDurationSec": 0.15,
    },
    "engine": {
        # VESC starter-cranking parameters. The engine.crank command reads
        # this; payload overrides at command time. All values are clamped
        # to hard safety ceilings in pi/engine.py regardless of config.
        "cranking": {
            # Absolute motor current commanded during cranking. VESC will
            # clamp to its own motor-current limit, so confirm VESC Tool's
            # configured max motor current is ≥ this value.
            "currentAmps": 65.0,
            # Per-attempt ceiling. Code clamps to 8s hard max.
            "maxDurationSec": 4.0,
            # Refresh rate for the SET_CURRENT command. VESC's command
            # timeout (default ~1s) requires we send faster than that.
            "refreshHz": 10,
            # Catch detection: engine considered caught when |motor_amps|
            # stays below currentAmps × this ratio for catchHoldMs.
            "catchCurrentRatio": 0.25,
            "catchHoldMs": 200,
            # Arming: catch detection only activates after the motor has
            # actually pulled ≥ currentAmps × this. Filters dry-runs
            # (no motor connected → no current draw → no false catch).
            "armCurrentRatio": 0.5,
        },
        # Full start choreography (engine.start command). Composes the
        # cranking primitive above with choke + spark management.
        "start": {
            # Servo preset used during cranking (choke closed for cold start).
            "chokePreset":           "start_cold",
            # Preset applied after engine catches (choke open).
            "runChokePreset":        "run",
            # Preset applied when cranking fails so we don't leave the
            # linkage in the closed/start position.
            "failChokePreset":       "idle",
            # Waveshare relay channel that controls the ignition / spark.
            # Channel 2 = "Aux 2" per the deployed wiring.
            "sparkRelayChannel":     2,
            # Pause between each phase, in milliseconds.
            "chokeSettleMs":         500,   # choke linkage to physically settle
            "sparkSettleMs":         200,   # spark coil to energize
            "postCatchSettleMs":     2000,  # engine to stabilize before opening choke
            # On crank failure, turn the spark relay back off so a dead
            # engine isn't sitting with the ignition energized.
            "turnOffSparkOnFailure": True,
        },
        # Regen charging (engine.charge command). Runs in a background
        # thread on the Pi; engine.stop is the only thing that can
        # interrupt it once started (other than the safety exits below).
        "charge": {
            # Amps to extract from the motor. Sent as a NEGATIVE
            # SET_CURRENT to the VESC (positive = drive, negative = regen).
            # Conservative default so the engine isn't bogged down before
            # we know what it can sustain.
            "currentAmps":      25.0,
            # Pack voltage to stop charging (charge complete). 15S LiFePO4
            # rests fully charged at ~52.8 V (3.52 V/cell, measured). Set
            # voltageStop just below that so the under-charge-load voltage
            # rise doesn't trip the BMS termination point. 52.5 V (3.50 V/cell)
            # lands at ~95–98 % SOC when the load releases.
            "voltageStop":      52.5,
            # Hard pack-protection floor (NOT an "engine not generating"
            # detector — minRpmForLoad already covers that). A big external
            # load sags the pack well below resting; aborting there only
            # drains it faster, so this sits near BMS-cutoff territory.
            # LiFePO4 BMS cuts ~2.5 V/cell = ~37.5 V on 15S; 42.0 V
            # (2.8 V/cell) keeps ~4.5 V headroom while clearing load sag.
            "voltageMinAbort":  42.0,
            # Hard ceiling on charge duration; engine auto-stops on timeout.
            # Code clamps to 2 hours; default to that full 2-hour window.
            "maxDurationSec":   7200,    # 2 hours
            "refreshHz":        10,
            # Engine RPM minimum. Bog-down protection — abort if the load
            # drags the engine below this.
            "minRpmForLoad":    800,
            # Temperature safety ceilings. Motor temp only respected when
            # the reading looks plausible (<250°C) — disconnected probes
            # report ~388°C and would trip every attempt.
            "maxFetTempC":      80.0,
            "maxMotorTempC":    100.0,
            # Soft-start window: the charge loop slews 0→currentAmps over this
            # many seconds (currentAmps/rampUpSec A/s) so the engine takes the
            # load on gradually instead of all at once. Also gates the settle
            # window before low-voltage / bog-down safety checks arm.
            "rampUpSec":        60.0,
        },
        # Graceful shutdown (engine.stop command).
        "stop": {
            # Motor RPM below this is considered "stopped".
            "rpmIdleThreshold":     100,
            # Max time we'll wait for the engine to coast down after
            # spark relay opens. After this, we publish "idle" regardless.
            "rpmWaitTimeoutSec":    15,
            # Mirror engine.start.sparkRelayChannel.
            "sparkRelayChannel":    2,
            # How long stop waits for the charge thread to release the
            # motor after signaling abort.
            "chargeJoinTimeoutSec": 5,
        },
        # Autonomous supervisor (engine_supervisor.py). Polls voltage and
        # orchestrates the start → charge → stop cycle hands-off. Disabled
        # by default; flip enabled=true to activate.
        "supervisor": {
            "enabled":              False,
            # Primary decision signal: SoC % from Predator LCD (coulomb
            # counted by the BMS — accurate under load, unlike voltage).
            # Supervisor pre-wakes the LCD at every tick so battery_soc is
            # fresh; voltage thresholds below are fallback when LCD asleep.
            "socStart":             25,    # ≤ this → auto-start (active window)
            "socCritical":          12,    # ≤ this → start even in quiet hours
            "socStop":              85,    # ≥ this → auto-stop (with load guard)

            # Load-aware proactive start (uses Predator BMS
            # time_to_empty_minutes). Fires before SoC actually drops to
            # socStart, but only if the pack is also at least somewhat
            # depleted — prevents firing on a brief load spike at high SoC.
            "runwayThresholdMin":   60,    # ≤ this min runway → consider proactive
            "proactiveSocCeiling":  60,    # … but only when SoC ≤ this %

            # Load-aware stop guard: don't auto-stop if BMS runway under
            # the current load is short — we'd just restart immediately.
            "sustainableRunwayMin": 180,   # only stop if runway ≥ this OR no load

            # Voltage fallback (15S LiFePO4 curve in src/lib/voltageSoc.ts).
            # Consulted only when battery_soc is null (LCD asleep + servo
            # wake failed). Stop threshold lives in engine.charge.voltageStop.
            "voltageStart":         48.0,
            "voltageCritical":      46.5,
                                            # (if allowQuietOverride is true)
            "tickIntervalSec":      15,
            "actionCooldownSec":    60,
            "respectQuietHours":    True,
        },
    },
    "charge": {
        "enabled": False,
        "preset": "daytime_only",
        "windows": [
            {"start": "08:00", "end": "18:00", "weekdays": [1, 2, 3, 4, 5, 6]},
        ],
        "socStart": 25,
        "socStop": 85,
        "socCritical": 12,
        "quietHours": {"start": "21:00", "end": "07:00"},
        "allowQuietOverride": False,
    },
}


def init_firestore() -> firestore.Client:
    if not os.path.exists(SERVICE_ACCOUNT):
        print(f"[fatal] service account not found at {SERVICE_ACCOUNT}", file=sys.stderr)
        sys.exit(1)
    if not firebase_admin._apps:
        cred = credentials.Certificate(SERVICE_ACCOUNT)
        firebase_admin.initialize_app(cred)
    return firestore.client()


def main() -> None:
    db = init_firestore()
    print(f"[seed] writing defaults for units/{UNIT_ID}/config/*")

    for name, payload in DEFAULTS.items():
        ref = db.document(f"units/{UNIT_ID}/config/{name}")
        snap = ref.get()
        if snap.exists:
            print(f"  config/{name:<8} — exists, skipping")
            continue
        ref.set(payload)
        print(f"  config/{name:<8} — created")

    print("[seed] done")


if __name__ == "__main__":
    main()
