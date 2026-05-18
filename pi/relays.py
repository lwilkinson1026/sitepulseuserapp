"""
Waveshare RPi Relay Board (B) controller + SPST physical-override switch.

Hardware:
  - Waveshare RPi Relay Board (B), 3 channels on BCM 5, 6, 13 (active-low).
  - SPST toggle from BCM 17 to GND, internal pull-up enabled. Switch closed
    → light forced ON regardless of app/sentry. Switch open → app/auto wins.

Channel 1 is the security light by convention (overridable via
`config/light.relayChannel` in Firestore).

Exposed handlers (imported lazily by command_listener.py):
    handle_light_set(db, unit_id, payload)
    handle_relay_set(db, unit_id, payload)

Also exposes `start_override_watcher(db, unit_id)` — a one-shot launcher
that spawns a polling thread to mirror the physical switch into Firestore.

Env overrides:
    SITEPULSE_RELAY_1_PIN / _2_PIN / _3_PIN   default 5 / 6 / 13
    SITEPULSE_OVERRIDE_PIN                    default 17
    SITEPULSE_RELAY_ACTIVE_LOW                "1" (default) or "0"
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional

from firebase_admin import firestore


# ─── hardware config ────────────────────────────────────────────────────────

RELAY_PINS: Dict[int, int] = {
    1: int(os.environ.get("SITEPULSE_RELAY_1_PIN", "5")),
    2: int(os.environ.get("SITEPULSE_RELAY_2_PIN", "6")),
    3: int(os.environ.get("SITEPULSE_RELAY_3_PIN", "13")),
}
OVERRIDE_PIN = int(os.environ.get("SITEPULSE_OVERRIDE_PIN", "17"))
RELAY_ACTIVE_LOW = os.environ.get("SITEPULSE_RELAY_ACTIVE_LOW", "1") == "1"


# ─── lazy GPIO import ───────────────────────────────────────────────────────
# Lets this module import on a Mac for typing / dry-run tests. Handlers
# raise at call time if GPIO isn't available.

_GPIO = None
_GPIO_IMPORT_ERR: Optional[str] = None

def _gpio():
    global _GPIO, _GPIO_IMPORT_ERR
    if _GPIO is not None:
        return _GPIO
    if _GPIO_IMPORT_ERR is not None:
        raise RuntimeError(f"RPi.GPIO unavailable: {_GPIO_IMPORT_ERR}")
    try:
        import RPi.GPIO as G   # type: ignore[import-not-found]
        _GPIO = G
        return _GPIO
    except Exception as e:
        _GPIO_IMPORT_ERR = str(e)
        raise RuntimeError(f"RPi.GPIO unavailable: {e}") from e


# ─── module state ───────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_initialized = False
_relay_logical: Dict[int, bool] = {1: False, 2: False, 3: False}
_override_active = False


def _on_value(on: bool) -> int:
    g = _gpio()
    if RELAY_ACTIVE_LOW:
        return g.LOW if on else g.HIGH
    return g.HIGH if on else g.LOW


def _ensure_initialized() -> None:
    global _initialized
    with _state_lock:
        if _initialized:
            return
        g = _gpio()
        g.setmode(g.BCM)
        g.setwarnings(False)
        for pin in RELAY_PINS.values():
            g.setup(pin, g.OUT)
            g.output(pin, _on_value(False))
        g.setup(OVERRIDE_PIN, g.IN, pull_up_down=g.PUD_UP)
        _initialized = True


def _drive(channel: int, on: bool) -> None:
    """Low-level: write the GPIO and update the logical mirror. Caller holds _state_lock."""
    _gpio().output(RELAY_PINS[channel], _on_value(on))
    _relay_logical[channel] = on


def _read_override() -> bool:
    g = _gpio()
    # PUD_UP + switch-to-GND: LOW means closed (override active).
    return g.input(OVERRIDE_PIN) == g.LOW


# ─── Firestore mirroring ────────────────────────────────────────────────────

def _light_channel(db: firestore.Client, unit_id: str) -> int:
    """Read which channel currently backs the security light. Defaults to 1."""
    try:
        snap = db.document(f"units/{unit_id}/config/light").get()
        if snap.exists:
            ch = snap.get("relayChannel")
            if ch in (1, 2, 3):
                return int(ch)
    except Exception:
        pass
    return 1


def _mirror_light(db: firestore.Client, unit_id: str, source: str) -> None:
    ch = _light_channel(db, unit_id)
    state_on = _override_active or _relay_logical[ch]
    db.document(f"units/{unit_id}/current/light").set({
        "state": state_on,
        "physicalOverride": _override_active,
        "lastChangedAt": firestore.SERVER_TIMESTAMP,
        "lastChangedBy": source,
    }, merge=True)


def _mirror_relays(db: firestore.Client, unit_id: str, source: str) -> None:
    """Mirror all three channels' current logical state."""
    db.document(f"units/{unit_id}/current/relays").set({
        "channels": {
            "1": {"state": _relay_logical[1]},
            "2": {"state": _relay_logical[2]},
            "3": {"state": _relay_logical[3]},
        },
        "lastChangedAt": firestore.SERVER_TIMESTAMP,
        "lastChangedBy": source,
    }, merge=True)


# ─── command handlers ──────────────────────────────────────────────────────

def handle_light_set(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """
    payload = { mode: 'off' | 'on' | 'auto' }
      or    = { configPatch: { ...LightConfig fields... } }   (e.g. autoTimeoutSec)
    """
    _ensure_initialized()
    ch = _light_channel(db, unit_id)

    if "configPatch" in payload:
        # Pure config update — don't touch the relay.
        patch = payload["configPatch"] or {}
        db.document(f"units/{unit_id}/config/light").set(patch, merge=True)
        return

    mode = payload.get("mode")
    if mode not in ("off", "on", "auto"):
        raise ValueError(f"light.set: invalid mode {mode!r}")

    with _state_lock:
        if _override_active:
            # Physical switch is forcing the light on. Persist the desired
            # mode so it takes effect once the switch releases.
            pass
        elif mode == "on":
            _drive(ch, True)
        else:
            # 'off' and 'auto' both leave the relay de-energized for now;
            # sentry (Phase D) will flip it on when motion fires in auto mode.
            _drive(ch, False)

        db.document(f"units/{unit_id}/config/light").set({"mode": mode}, merge=True)
        db.document(f"units/{unit_id}/config/relays").set(
            {"channels": {str(ch): {"mode": mode}}}, merge=True,
        )
        _mirror_light(db, unit_id, "app")
        _mirror_relays(db, unit_id, "app")


def handle_relay_set(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """
    payload = { channel: 1|2|3, mode: 'off' | 'on' | 'auto' }
      or    = { configReplace: { ...RelaysConfig... } }     (label edits etc.)
    """
    _ensure_initialized()

    if "configReplace" in payload:
        config = payload["configReplace"] or {}
        db.document(f"units/{unit_id}/config/relays").set(config, merge=True)
        return

    channel = payload.get("channel")
    mode = payload.get("mode")
    if channel not in (1, 2, 3):
        raise ValueError(f"relay.set: invalid channel {channel!r}")
    if mode not in ("off", "on", "auto"):
        raise ValueError(f"relay.set: invalid mode {mode!r}")

    light_ch = _light_channel(db, unit_id)

    with _state_lock:
        if channel == light_ch and _override_active and mode != "on":
            # Light is force-on by hardware; ignore the relay change but
            # remember the user's choice for when override releases.
            pass
        elif mode == "on":
            _drive(channel, True)
        else:
            _drive(channel, False)

        db.document(f"units/{unit_id}/config/relays").set(
            {"channels": {str(channel): {"mode": mode}}}, merge=True,
        )
        if channel == light_ch:
            db.document(f"units/{unit_id}/config/light").set({"mode": mode}, merge=True)
            _mirror_light(db, unit_id, "app")
        _mirror_relays(db, unit_id, "app")


# ─── physical override watcher ─────────────────────────────────────────────

_watcher_thread: Optional[threading.Thread] = None

def start_override_watcher(db: firestore.Client, unit_id: str) -> threading.Thread:
    """
    Spawn (once) a polling thread that mirrors the SPST override switch into
    Firestore. Returns the running thread. Safe to call multiple times — only
    the first call starts a thread, subsequent calls return the existing one.
    """
    global _watcher_thread
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return _watcher_thread

    _ensure_initialized()

    def loop() -> None:
        global _override_active
        last_state: Optional[bool] = None
        # Write the initial state so the app sees the switch position
        # even if it never changes.
        last_published = 0.0
        while True:
            try:
                current = _read_override()
                changed = current != last_state
                if changed:
                    light_ch = _light_channel(db, unit_id)
                    with _state_lock:
                        _override_active = current
                        if current:
                            # Force the light on hardware-side. Keep the
                            # logical state so we can restore on release.
                            _drive(light_ch, True)
                        else:
                            # Switch released — fall back to the last
                            # commanded mode from config.
                            try:
                                snap = db.document(
                                    f"units/{unit_id}/config/relays"
                                ).get()
                                if snap.exists:
                                    mode = (snap.get("channels") or {}) \
                                        .get(str(light_ch), {}) \
                                        .get("mode", "off")
                                else:
                                    mode = "off"
                            except Exception:
                                mode = "off"
                            _drive(light_ch, mode == "on")
                        _mirror_light(db, unit_id, "physical")
                        _mirror_relays(db, unit_id, "physical")
                    last_state = current
                    last_published = time.monotonic()
                elif last_state is None or (time.monotonic() - last_published > 60):
                    # Heartbeat — mirror state at least every 60s so a
                    # stale `lastChangedAt` doesn't make the app think
                    # the watcher died.
                    with _state_lock:
                        _override_active = current
                        _mirror_light(db, unit_id, "physical")
                    last_state = current
                    last_published = time.monotonic()
                time.sleep(0.1)
            except Exception as e:
                print(f"[relays] override watcher error: {e}", flush=True)
                time.sleep(1)

    _watcher_thread = threading.Thread(
        target=loop, daemon=True, name="override-watcher",
    )
    _watcher_thread.start()
    return _watcher_thread


def pulse_light(db: firestore.Client, unit_id: str, duration_s: int) -> None:
    """Turn the security-light relay on for duration_s seconds, then off.
    Used by sentry on motion (autoLight + scareMode). Runs in a daemon
    thread so the caller isn't blocked. Respects the physical override —
    if the SPST switch is forcing the light on, we don't fight it. After
    the duration, defers to config/light.mode: if the user has manually
    set 'on', we leave it on; otherwise we turn it off."""
    _ensure_initialized()

    def run() -> None:
        ch = _light_channel(db, unit_id)
        with _state_lock:
            if _override_active:
                return  # hardware switch already controlling
            _drive(ch, True)
            _mirror_light(db, unit_id, "sentry")
            _mirror_relays(db, unit_id, "sentry")

        time.sleep(max(1, duration_s))

        with _state_lock:
            if _override_active:
                return
            # Respect user intent — if config/light.mode is 'on', don't
            # cut the light off after the pulse.
            try:
                snap = db.document(f"units/{unit_id}/config/light").get()
                mode = (snap.to_dict() or {}).get("mode", "auto") if snap.exists else "auto"
            except Exception:
                mode = "auto"
            if mode != "on":
                _drive(ch, False)
                _mirror_light(db, unit_id, "sentry")
                _mirror_relays(db, unit_id, "sentry")

    threading.Thread(target=run, daemon=True, name="light-pulse").start()


def cleanup() -> None:
    """Drive all relays off and release GPIO. Called on shutdown."""
    if not _initialized:
        return
    try:
        g = _gpio()
        for pin in RELAY_PINS.values():
            try:
                g.output(pin, _on_value(False))
            except Exception:
                pass
        g.cleanup()
    except Exception:
        pass
