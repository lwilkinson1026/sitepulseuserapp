"""
GPIO button presser for the Predator's front panel.

Replaces the two PCA9685 "button" / "ac" servo arms with electrical presses.
Each channel is a Pi GPIO driving the LED side of a PC817 optocoupler through
330 Ω; the opto's phototransistor sits across the button's PCB pads
(collector → signal pad, emitter → ground), so lighting the LED is
electrically identical to a finger pressing the button. No moving parts, no
end-stop calibration, no servo channel map to get wrong.

Channels (BCM numbering, header pin in brackets):
  lcd   GPIO 24 (18)   Predator LCD wake button    env SITEPULSE_BTN_LCD_PIN
  ac    GPIO 16 (36)   Predator AC output toggle   env SITEPULSE_BTN_AC_PIN

A press is: drive high, hold, drive low. Hold time is clamped to
[0.05, 2.0] s exactly as the servo version was, so config/lcdWake and the
command payloads keep their meaning. Proven on UNIT-001 2026-09-15: a 0.4 s
press takes the panel from dark to awake within 3 s.

Exposed handlers (imported lazily by command_listener.py). Same names and
signatures as the servo version, so nothing upstream had to change:
    handle_lcd_wake(db, unit_id, payload)
    handle_ac_toggle(db, unit_id, payload)
    wake_lcd(db, unit_id, press_duration_s=None, burst_publish=False)
    press_ac(db, unit_id, press_duration_s=None)

Lifecycle:
    start_buttons(db, unit_id)          — boot: claim pins, park them released
    start_lcd_wake_loop(db, unit_id)    — boot: periodic LCD-wake presser
                                           (opt-in via config/lcdWake.enabled)
    release_all()                       — shutdown: release pins, never exit pressed

Hardware notes:
  - Raw lgpio on gpiochip0 (Pi 5, kernel >= 6.6), same as the I²C sniffer.
    A pin claim failure is a clear error here, not rpi-lgpio's
    read-before-claim quirk.
  - pi/bench_button_sim.py drives the same pins and cannot run while the
    listener holds them ('GPIO busy'). Stop the listener first.
  - The AC button has no read-back: each press flips the outlet and the Pi
    cannot tell which way. The LCD decoder's ac_active is the only signal,
    and on UNIT-001 it has not yet been seen to follow a press.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional

from firebase_admin import firestore

import burst


CHIP = 0
PRESS_MIN_S, PRESS_MAX_S = 0.05, 2.0

LCD_WAKE_DEFAULT_PRESS_S = 0.4   # quick tap; what the panel was proven with
AC_TOGGLE_DEFAULT_PRESS_S = 0.5  # a touch longer for the inverter button

CHANNELS: Dict[str, int] = {
    "lcd": int(os.environ.get("SITEPULSE_BTN_LCD_PIN", "24")),
    "ac":  int(os.environ.get("SITEPULSE_BTN_AC_PIN", "16")),
}

# Periodic LCD-wake loop config. Lives in its own Firestore doc
# (config/lcdWake) so toggling the loop on/off is a one-field write.
LCD_WAKE_DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "intervalSec": 600,          # 10 min — matches the Predator LCD sleep timer
    "pressDurationSec": LCD_WAKE_DEFAULT_PRESS_S,
}


# ─── hardware ──────────────────────────────────────────────────────────────

_lgpio: Any = None
_h: Optional[int] = None
_claimed: Dict[str, int] = {}
_lock = threading.Lock()          # serialises presses across threads
_stop = threading.Event()
_last_press: Dict[str, Dict[str, Any]] = {}


def _ensure_initialized() -> None:
    """Open the chip and claim every channel as an output parked low.
    Lazy so this module imports on a Mac without lgpio."""
    global _lgpio, _h
    if _h is not None:
        return
    import lgpio  # type: ignore[import-not-found]
    _lgpio = lgpio
    h = lgpio.gpiochip_open(CHIP)
    try:
        for name, pin in CHANNELS.items():
            rc = lgpio.gpio_claim_output(h, pin, 0)
            if rc < 0:
                raise RuntimeError(
                    f"buttons: claim GPIO{pin} ({name}) failed rc={rc} — "
                    f"is bench_button_sim.py or another process holding it?"
                )
            _claimed[name] = pin
    except Exception:
        for pin in _claimed.values():
            try:
                lgpio.gpio_free(h, pin)
            except Exception:
                pass
        _claimed.clear()
        lgpio.gpiochip_close(h)
        raise
    _h = h


def _press(name: str, hold_s: float) -> float:
    """Drive one channel high for hold_s, then low. Returns the actual hold.
    The release is in a finally so no exception can leave a button pressed."""
    _ensure_initialized()
    pin = _claimed[name]
    hold_s = max(PRESS_MIN_S, min(PRESS_MAX_S, float(hold_s)))
    with _lock:
        t0 = time.monotonic()
        _lgpio.gpio_write(_h, pin, 1)
        try:
            time.sleep(hold_s)
        finally:
            _lgpio.gpio_write(_h, pin, 0)
        held = time.monotonic() - t0
        _last_press[name] = {"holdSec": round(held, 3), "monotonic": t0}
    return held


def _mirror(db: firestore.Client, unit_id: str, name: str, held_s: float) -> None:
    """Record the press in current/buttons for the app / debugging. One write
    per press; a failed write must not fail the press, so it only logs."""
    try:
        db.document(f"units/{unit_id}/current/buttons").set({
            name: {
                "pin": _claimed.get(name, CHANNELS[name]),
                "lastHoldSec": round(held_s, 3),
                "lastPressAt": firestore.SERVER_TIMESTAMP,
            },
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }, merge=True)
    except Exception as e:
        print(f"[buttons] mirror failed: {e}", flush=True)


# ─── lifecycle ─────────────────────────────────────────────────────────────

def start_buttons(db: firestore.Client, unit_id: str) -> None:
    """Claim the pins at boot so a wiring conflict shows up in the log
    immediately rather than on the first press."""
    _ensure_initialized()
    try:
        db.document(f"units/{unit_id}/current/buttons").set({
            "pins": dict(_claimed),
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }, merge=True)
    except Exception as e:
        print(f"[buttons] mirror failed: {e}", flush=True)
    print(
        "[buttons] started; " + ", ".join(f"{n} GPIO{p}" for n, p in _claimed.items()),
        flush=True,
    )


def release_all() -> None:
    """Stop the wake loop, drive every channel low, free the pins."""
    global _h
    _stop.set()
    t = _lcd_wake_thread
    if t is not None and t.is_alive():
        t.join(timeout=1.0)
    if _h is None:
        return
    with _lock:
        for pin in _claimed.values():
            try:
                _lgpio.gpio_write(_h, pin, 0)
                _lgpio.gpio_free(_h, pin)
            except Exception as e:
                print(f"[buttons] release GPIO{pin} failed: {e}", flush=True)
        _claimed.clear()
        try:
            _lgpio.gpiochip_close(_h)
        finally:
            _h = None


# ─── LCD wake ──────────────────────────────────────────────────────────────

def _load_lcd_wake_config(db: firestore.Client, unit_id: str) -> Dict[str, Any]:
    """Read config/lcdWake, falling back to defaults if missing/unreadable."""
    base = dict(LCD_WAKE_DEFAULT_CONFIG)
    try:
        snap = db.document(f"units/{unit_id}/config/lcdWake").get()
        if snap.exists:
            base.update(snap.to_dict() or {})
    except Exception as e:
        print(f"[buttons] lcdWake config read failed: {e}", flush=True)
    return base


def wake_lcd(
    db: firestore.Client,
    unit_id: str,
    press_duration_s: Optional[float] = None,
    burst_publish: bool = False,
) -> None:
    """Press and release the LCD-wake button.

    Used by the periodic loop, the `lcd.wake` command, and the engine
    supervisor's pre-read wake. `press_duration_s` is clamped to
    [0.05, 2.0]; None falls back to config/lcdWake.pressDurationSec.

    `burst_publish` asks the telemetry publisher to raise its cadence for a
    short window (see burst.py). It defaults to False and MUST stay that way
    for the unattended loop: a burst on every periodic wake would blow the
    Firestore free-tier write budget. Bursting is for presses a human is
    actually waiting on.
    """
    if burst_publish:
        # Signalled before the press so the publisher's first fast tick lands
        # while the panel is lighting up.
        burst.request(f"lcd.wake {unit_id}")
    if press_duration_s is None:
        press_duration_s = float(
            _load_lcd_wake_config(db, unit_id).get("pressDurationSec", LCD_WAKE_DEFAULT_PRESS_S)
        )
    held = _press("lcd", press_duration_s)
    _mirror(db, unit_id, "lcd", held)


def handle_lcd_wake(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """payload = {} or {pressDurationSec: 0.4}. Command path only: a person
    tapped Wake LCD and is watching for the readout, so burst."""
    dur = payload.get("pressDurationSec")
    wake_lcd(db, unit_id, float(dur) if dur is not None else None, burst_publish=True)


_lcd_wake_thread: Optional[threading.Thread] = None


def _lcd_wake_loop(db: firestore.Client, unit_id: str) -> None:
    """Periodically press the LCD wake button. Re-reads config every pass so
    toggling `enabled` or `intervalSec` takes effect within ~10 s."""
    last_wake = 0.0
    while not _stop.is_set():
        cfg = _load_lcd_wake_config(db, unit_id)
        enabled = bool(cfg.get("enabled", False))
        interval = max(60, int(cfg.get("intervalSec", 600)))
        if not enabled:
            if _stop.wait(10):
                return
            continue
        now = time.monotonic()
        if now - last_wake >= interval:
            try:
                wake_lcd(db, unit_id, float(cfg.get("pressDurationSec", LCD_WAKE_DEFAULT_PRESS_S)))
                last_wake = time.monotonic()
            except Exception as e:
                print(f"[buttons] lcd_wake_loop error: {e}", flush=True)
                if _stop.wait(30):
                    return
                continue
        remaining = max(1.0, interval - (time.monotonic() - last_wake))
        if _stop.wait(min(10.0, remaining)):
            return


def start_lcd_wake_loop(db: firestore.Client, unit_id: str) -> None:
    """Spawn the LCD-wake loop (idempotent). Dormant until
    config/lcdWake.enabled is true."""
    global _lcd_wake_thread
    if _lcd_wake_thread is not None and _lcd_wake_thread.is_alive():
        return
    _stop.clear()
    _lcd_wake_thread = threading.Thread(
        target=_lcd_wake_loop, args=(db, unit_id), daemon=True, name="lcd-wake-loop",
    )
    _lcd_wake_thread.start()
    print("[buttons] lcd-wake loop started (config/lcdWake.enabled gates presses)", flush=True)


# ─── AC toggle ─────────────────────────────────────────────────────────────

def press_ac(
    db: firestore.Client,
    unit_id: str,
    press_duration_s: Optional[float] = None,
) -> None:
    """Press and release the AC toggle button once. Each call flips the
    outlet state; there is no read-back. None uses AC_TOGGLE_DEFAULT_PRESS_S."""
    if press_duration_s is None:
        press_duration_s = AC_TOGGLE_DEFAULT_PRESS_S
    held = _press("ac", press_duration_s)
    _mirror(db, unit_id, "ac", held)


def handle_ac_toggle(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """payload = {} or {pressDurationSec: 0.5}"""
    dur = payload.get("pressDurationSec")
    press_ac(db, unit_id, float(dur) if dur is not None else None)
