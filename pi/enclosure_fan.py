"""
Thermostat for the electronics-enclosure fan.

Why this exists
───────────────
The enclosure fan used to share the engine-fan relay, so it only ran while the
engine did. Sitting outside in the sun the Pi cooks with the engine OFF — the
heat is solar, not engine. The fan needs to follow the Pi's temperature.

Running it 24/7 instead is not free: a 48 V fan is tens of watts straight out
of the pack, which buys extra engine runs just to feed a fan. Hence a
thermostat rather than a jumper.

Behaviour (config/enclosureFan.mode)
────────────────────────────────────
  auto  on at >= onTempC, off at <= offTempC (hysteresis between). Also on
        whenever the engine is running/charging — what the shared relay did.
  on    always on.
  off   off — but still forced on at >= criticalTempC. "Off" is for quiet
        bench work, not permission to cook the Pi.

If the temperature cannot be read, the fan runs. Every failure mode here
resolves to "fan on".

Wiring
──────
Put the fan on the relay's NC contact and list the channel in
SITEPULSE_RELAY_INVERT, as the engine fans on ch3 already are. Then a dead
listener, a hung Pi, or a Pi that has not booted yet all leave the relay
de-energized and the fan RUNNING. On the NO contact a crashed listener on a
hot day means no cooling.

The feature is inert until config/enclosureFan.relayChannel is set, so units
without a relay-switched enclosure fan are unaffected. The temperature is the
same SoC sensor pi_health publishes as pi_temp_c.

Config (units/<id>/config/enclosureFan), all optional except relayChannel:
    relayChannel   1|2|3    channel that switches the fan. Unset → disabled.
    mode           'auto'   | 'on' | 'off'
    onTempC        60
    offTempC       50
    criticalTempC  75       forces the fan on even in 'off'

State (units/<id>/current/enclosureFan), written on change only:
    state, reason, tempC, lastChangedAt

Env overrides:
    SITEPULSE_ENCLOSURE_FAN_POLL_S     default 10  — temperature poll
    SITEPULSE_ENCLOSURE_FAN_CONFIG_S   default 60  — config/engine re-read
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

from firebase_admin import firestore

import pi_health

POLL_S = float(os.environ.get("SITEPULSE_ENCLOSURE_FAN_POLL_S", "10"))
# Firestore reads, not the sysfs read, are what this bounds — the temperature
# is polled every POLL_S regardless.
CONFIG_REFRESH_S = float(os.environ.get("SITEPULSE_ENCLOSURE_FAN_CONFIG_S", "60"))

DEFAULTS: Dict[str, Any] = {
    "mode": "auto",
    "onTempC": 60.0,
    "offTempC": 50.0,
    "criticalTempC": 75.0,
}

_poke = threading.Event()
_thread: Optional[threading.Thread] = None


def poke() -> None:
    """Re-read config and re-evaluate now (called after a relay.set on the
    enclosure-fan channel, so the app's mode change lands immediately)."""
    _poke.set()


def decide(
    cfg: Dict[str, Any],
    temp_c: Optional[float],
    engine_running: bool,
    currently_on: bool,
) -> Tuple[bool, str]:
    """Pure thermostat decision → (fan_on, reason). No I/O, so it is testable
    off the Pi."""
    mode = cfg.get("mode", "auto")
    if mode == "on":
        return True, "manual_on"
    if temp_c is None:
        return True, "temp_unavailable"
    if temp_c >= float(cfg["criticalTempC"]):
        return True, "critical_temp"
    if mode == "off":
        return False, "manual_off"
    if engine_running:
        return True, "engine_running"
    on_c, off_c = float(cfg["onTempC"]), float(cfg["offTempC"])
    if off_c >= on_c:
        # Misconfigured band would chatter the relay; fall back to a sane gap.
        off_c = on_c - 5.0
    if temp_c >= on_c:
        return True, "hot"
    if temp_c <= off_c:
        return False, "cool"
    # Inside the band: hold whatever we were doing.
    return currently_on, "hot" if currently_on else "cool"


def _load(db: firestore.Client, unit_id: str) -> Tuple[Optional[Dict[str, Any]], bool]:
    """→ (config or None if the feature is disabled, engine_running)."""
    from relays import _enclosure_fan_channel, _engine_is_running
    if _enclosure_fan_channel(db, unit_id) is None:
        return None, False
    cfg = dict(DEFAULTS)
    try:
        snap = db.document(f"units/{unit_id}/config/enclosureFan").get()
        raw = (snap.to_dict() or {}) if snap.exists else {}
        for key in ("onTempC", "offTempC", "criticalTempC"):
            if isinstance(raw.get(key), (int, float)):
                cfg[key] = float(raw[key])
        if raw.get("mode") in ("auto", "on", "off"):
            cfg["mode"] = raw["mode"]
    except Exception as e:
        print(f"[enclosure_fan] config read failed, using defaults: {e}", flush=True)
    return cfg, _engine_is_running(db, unit_id)


def start_enclosure_fan(db: firestore.Client, unit_id: str) -> threading.Thread:
    """Spawn (once) the thermostat thread. Safe to start unconditionally: it
    idles until config/enclosureFan.relayChannel is set."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread

    def loop() -> None:
        from relays import drive_enclosure_fan
        cfg: Optional[Dict[str, Any]] = None
        engine_running = False
        loaded_at = 0.0
        # None = not driven yet, so the first pass always writes the relay
        # and publishes, whatever state the channel booted in.
        fan_on: Optional[bool] = None
        last_reason: Optional[str] = None
        while True:
            try:
                poked = _poke.is_set()
                _poke.clear()
                if poked or time.monotonic() - loaded_at >= CONFIG_REFRESH_S:
                    cfg, engine_running = _load(db, unit_id)
                    loaded_at = time.monotonic()
                if cfg is not None:
                    temp_c = pi_health.cpu_temp_c()
                    want, reason = decide(cfg, temp_c, engine_running, bool(fan_on))
                    if want != fan_on or reason != last_reason:
                        drive_enclosure_fan(db, unit_id, want)
                        db.document(f"units/{unit_id}/current/enclosureFan").set({
                            "state": want,
                            "reason": reason,
                            "tempC": temp_c,
                            "lastChangedAt": firestore.SERVER_TIMESTAMP,
                        })
                        print(f"[enclosure_fan] {'ON' if want else 'OFF'} "
                              f"({reason}, {temp_c} °C)", flush=True)
                        fan_on, last_reason = want, reason
            except Exception as e:
                print(f"[enclosure_fan] loop error: {e}", flush=True)
            _poke.wait(POLL_S)

    _thread = threading.Thread(target=loop, daemon=True, name="enclosure-fan")
    _thread.start()
    return _thread
