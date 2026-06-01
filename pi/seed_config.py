"""
Seed default config docs at units/{UNIT_ID}/config/* on first boot.

Idempotent: each doc is written with `merge=True` only when it doesn't
exist yet, so re-running this never clobbers user edits.

Usage:
    SITEPULSE_SA=~/sitepulse/service-account.json python3 seed_config.py

Defaults match the locked-in decisions documented in the feature plan:
  - Waveshare RPi Relay Board (B), 3 channels; channel 1 = security light
  - LiFePO4 14S thresholds: socStart 25, socStop 85, socCritical 12
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
            "3": {"label": "User Aux Output",  "mode": "off"},
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
        # Two servos:
        #   choke  — PCA9685 ch1, engine choke linkage (calibrate end-stops
        #            against the real linkage; pre-linkage bench seed is
        #            1333-1667 us = 60-120 deg in the 1000-2000 us mapping).
        #   button — PCA9685 ch2, micro servo arm that taps the Predator
        #            LCD wake button to defeat its 10-min sleep timer.
        #            1000-2000 us is a safe-but-loose default; calibrate
        #            with `python3 servo_test.py 2` before mounting the arm.
        "choke":  {"channel": 1, "minUs": 1333, "maxUs": 1667, "slewRate": 2.0,
                   "defaultOnStart": 0.0},
        "button": {"channel": 2, "minUs": 1000, "maxUs": 2000, "slewRate": 10.0,
                   "defaultOnStart": 0.0},
        "presets": {
            "idle":       {"choke": 0.0},
            "start_cold": {"choke": 1.0},
            "start_warm": {"choke": 0.3},
            "run":        {"choke": 0.0},
            "press":      {"button": 1.0},
            "release":    {"button": 0.0},
        },
    },
    "lcdWake": {
        # Periodic LCD wake-button presser. Opt-in: stays off until the
        # user calibrates the button servo and explicitly enables it
        # (either from the app or by setting enabled=true in Firestore).
        # Interval matches the Predator's ~10 min display sleep timer.
        "enabled": False,
        "intervalSec": 600,
        "pressDurationSec": 0.15,
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
