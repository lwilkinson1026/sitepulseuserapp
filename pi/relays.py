"""
Waveshare RPi Relay Board controller + SPST physical-override switch.

Hardware:
  - Waveshare RPi Relay Board (model D-1226 / original 3-channel),
    BCM 26, 20, 13 (active-low). NOTE: the "RPi Relay Board (B)" model
    uses BCM 5, 6, 13 instead — set the env vars below if you swap.
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
    1: int(os.environ.get("SITEPULSE_RELAY_1_PIN", "26")),
    2: int(os.environ.get("SITEPULSE_RELAY_2_PIN", "20")),
    3: int(os.environ.get("SITEPULSE_RELAY_3_PIN", "21")),
}
OVERRIDE_PIN = int(os.environ.get("SITEPULSE_OVERRIDE_PIN", "17"))
RELAY_ACTIVE_LOW = os.environ.get("SITEPULSE_RELAY_ACTIVE_LOW", "1") == "1"

# ─── TEMPORARY per-channel polarity invert (2026-06-17) ─────────────────────
# Channel 3's cooling fans are still wired to the relay's NC contact (not yet
# moved to NO). With normal polarity, logical "on" (engine running) energizes
# the relay → NC opens → fans cut out *while the engine is hot*. That's
# backwards and dangerous during testing. Inverting ch3 here flips its
# physical drive only: logical "on" de-energizes the relay → NC stays closed
# → fans run; logical "off" energizes → fans off. Channels 1 (light) and 2
# (spark) are unaffected and keep the board-wide active-low behavior.
#
# REMOVE this (set SITEPULSE_RELAY_INVERT="") once the fans are rewired to the
# NO contact. Override the inverted-channel set via the env var if needed.
_INVERT_ENV = os.environ.get("SITEPULSE_RELAY_INVERT", "3")
RELAY_INVERT = {int(c) for c in _INVERT_ENV.split(",") if c.strip().isdigit()}


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

# Engine states (current/engine.state) in which the engine is physically
# turning. Non-light aux channels in 'auto' mode are energized while the
# engine is in one of these states. 'charging' is included because the
# engine is still running during the regen/charge phase.
ENGINE_RUNNING_STATES = ("running", "charging")


def _on_value(on: bool, channel: Optional[int] = None) -> int:
    g = _gpio()
    active_low = RELAY_ACTIVE_LOW
    if channel in RELAY_INVERT:
        # Flip the electrical polarity for this channel only (see
        # RELAY_INVERT note above). Logical state is unchanged.
        active_low = not active_low
    if active_low:
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
        for ch, pin in RELAY_PINS.items():
            # initial= sidesteps rpi-lgpio's read-before-claim bug on Pi 5
            # ("GPIO not allocated" when setup() tries to sample current state
            # before the pin is claimed as output). Any explicit initial value
            # skips that read entirely. Use the channel's logical-"off" level
            # so inverted channels (RELAY_INVERT) come up de-energized-correct
            # rather than blipping the wrong way during boot.
            off_val = _on_value(False, ch)
            g.setup(pin, g.OUT, initial=off_val)
            g.output(pin, off_val)
        g.setup(OVERRIDE_PIN, g.IN, pull_up_down=g.PUD_UP)
        _initialized = True


def _drive(channel: int, on: bool) -> None:
    """Low-level: write the GPIO and update the logical mirror. Caller holds _state_lock."""
    _gpio().output(RELAY_PINS[channel], _on_value(on, channel))
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


# ─── engine-follow (auto aux channels) ─────────────────────────────────────

def _engine_is_running(db: firestore.Client, unit_id: str) -> bool:
    try:
        snap = db.document(f"units/{unit_id}/current/engine").get()
        if snap.exists:
            return snap.get("state") in ENGINE_RUNNING_STATES
    except Exception:
        pass
    return False


def reconcile_engine_follow(
    db: firestore.Client,
    unit_id: str,
    engine_state: Optional[str] = None,
) -> None:
    """Drive every non-light aux channel whose mode is 'auto' to match whether
    the engine is running. Called on each engine state transition (from
    engine.py's _publish_state) and when a channel is switched into 'auto'.

    `engine_state` is the freshly-published state when the caller already has
    it; otherwise we read current/engine ourselves.
    """
    _ensure_initialized()
    running = (
        engine_state in ENGINE_RUNNING_STATES
        if engine_state is not None
        else _engine_is_running(db, unit_id)
    )
    light_ch = _light_channel(db, unit_id)
    try:
        snap = db.document(f"units/{unit_id}/config/relays").get()
        channels = (snap.to_dict() or {}).get("channels", {}) if snap.exists else {}
    except Exception:
        channels = {}

    changed = False
    with _state_lock:
        for ch in RELAY_PINS:
            if ch == light_ch:
                continue
            if channels.get(str(ch), {}).get("mode") == "auto" and _relay_logical[ch] != running:
                _drive(ch, running)
                changed = True
        if changed:
            _mirror_relays(db, unit_id, "engine")


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
    # For a non-light channel switched into 'auto' (engine-follow), seed its
    # state from the current engine state right now — read before taking the
    # lock so we don't do Firestore I/O while holding it.
    engine_running = (
        _engine_is_running(db, unit_id)
        if (mode == "auto" and channel != light_ch)
        else False
    )

    with _state_lock:
        if channel == light_ch and _override_active and mode != "on":
            # Light is force-on by hardware; ignore the relay change but
            # remember the user's choice for when override releases.
            pass
        elif mode == "on":
            _drive(channel, True)
        elif mode == "auto" and channel != light_ch:
            # Engine-follow: energized only while the engine is running.
            _drive(channel, engine_running)
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
        for ch, pin in RELAY_PINS.items():
            try:
                g.output(pin, _on_value(False, ch))
            except Exception:
                pass
        g.cleanup()
    except Exception:
        pass
