"""
PCA9685 (V1246) servo controller.

Three servos:
  - "choke"  on PCA9685 ch1 — engine choke linkage. 0.0 = open/run,
             1.0 = closed/cold-start. If the linkage flips polarity,
             swap minUs/maxUs in config rather than negating positions.
  - "button" on PCA9685 ch2 — micro servo with an arm that pushes the
             Predator LCD's wake button so the screen doesn't sleep
             during active monitoring. 0.0 = released, 1.0 = pressed.
  - "ac"     on PCA9685 ch3 — micro servo arm that taps the Predator's
             AC power toggle button. Each press flips the AC outlet
             state (on→off or off→on); the servo can't tell which.
             0.0 = released, 1.0 = pressed.

Positions are normalized 0.0-1.0. Per-servo pulse-width range
(minUs/maxUs) maps the normalized value to actual PWM via the
adafruit_motor.servo.Servo class (angle = pos * 180).

Exposed handlers (imported lazily by command_listener.py):
    handle_servo_set(db, unit_id, payload)
    handle_servo_preset(db, unit_id, payload)
    handle_servo_update(db, unit_id, payload)
    handle_lcd_wake(db, unit_id, payload)
    handle_ac_toggle(db, unit_id, payload)

Lifecycle:
    start_servos(db, unit_id)          — boot: drive defaults, start slew loop.
    start_lcd_wake_loop(db, unit_id)   — boot: periodic LCD-wake presser
                                          (opt-in via config/lcdWake.enabled).
    detach_all()                       — shutdown: cut PWM so servos go limp.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

from firebase_admin import firestore


SERVO_NAMES = ("choke", "button", "ac")
PWM_FREQ_HZ = 50          # standard hobby servo
TICK_HZ = 50              # slew loop tick rate

# How often the slew loop may mirror state to Firestore *while something is
# actually moving*. 1 Hz is what makes the app's servo readout feel live
# during a choke sweep or a button press.
MIRROR_PERIOD_S = 1.0

# How often to mirror when nothing has changed at all — a pure liveness
# heartbeat.
#
# This distinction did not exist before 2026-08-15: the loop wrote
# current/servos unconditionally every second, whether or not any value
# differed, which is 86,400 writes/day/unit against a 20,000/day free-tier
# ceiling. Servos are at rest essentially all the time, so almost every one
# of those writes re-stated a value Firestore already held. This was the
# single largest consumer in the system — larger than the telemetry
# publisher it sat quietly beside.
MIRROR_IDLE_PERIOD_S = 120.0


# ─── lazy hardware import ──────────────────────────────────────────────────

_HW: Optional[Dict[str, Any]] = None

def _hw() -> Dict[str, Any]:
    global _HW
    if _HW is not None:
        return _HW
    try:
        import board                      # type: ignore[import-not-found]
        import busio                      # type: ignore[import-not-found]
        from adafruit_pca9685 import PCA9685     # type: ignore[import-not-found]
        from adafruit_motor import servo as servo_mod  # type: ignore[import-not-found]
        _HW = {
            "board": board, "busio": busio,
            "PCA9685": PCA9685, "servo_mod": servo_mod,
        }
        return _HW
    except Exception as e:
        raise RuntimeError(f"PCA9685 dependencies unavailable: {e}") from e


# ─── module state ──────────────────────────────────────────────────────────

_lock = threading.Lock()
_initialized = False
_pca: Any = None
_servos: Dict[str, Any] = {}
_channels: Dict[str, int] = {}
_pulse_us: Dict[str, Tuple[int, int]] = {}
_slew_rate: Dict[str, float] = {}
_default_on_start: Dict[str, float] = {}
_position: Dict[str, float] = {}
_target: Dict[str, float] = {}
_last_commanded_us: Dict[str, int] = {}
_last_command_at: float = 0.0
_slew_thread: Optional[threading.Thread] = None
_stop = threading.Event()


# ─── config loader ─────────────────────────────────────────────────────────

DEFAULT_CONFIG: Dict[str, Any] = {
    "choke": {"channel": 1, "minUs": 1333, "maxUs": 1667, "slewRate": 2.0,
              "defaultOnStart": 0.0},
    # LCD-wake button presser. Calibrate end-stops with `servo_test.py 2`
    # before mounting the arm; the 1000-2000 us default is a safe-but-loose
    # range that won't strain a typical SG90 but may overshoot the button.
    # Slew rate 10.0/sec snaps the full range in ~100 ms (feels like a press,
    # not a sweep). Default position 0.0 = released.
    "button": {"channel": 2, "minUs": 1000, "maxUs": 2000, "slewRate": 10.0,
               "defaultOnStart": 0.0},
    # AC-toggle button presser. Same shape as "button" — calibrate with
    # `servo_test.py 3` before mounting. Note: this servo toggles AC power
    # to whatever's plugged into the Predator's outlets; each press flips
    # the state. The Pi has no way to know which state we're in.
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
}


# Periodic LCD-wake loop config. Lives in its own Firestore doc
# (config/lcdWake) so toggling the loop on/off doesn't touch servo config.
LCD_WAKE_DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,          # opt-in: calibrate the button servo first
    "intervalSec": 600,        # 10 min — matches Predator LCD sleep timer
    "pressDurationSec": 0.15,  # quick tap (~human finger press)
}


def _load_config(db: firestore.Client, unit_id: str) -> Dict[str, Any]:
    snap = db.document(f"units/{unit_id}/config/servos").get()
    if not snap.exists:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_CONFIG.items()}
    cfg = snap.to_dict() or {}
    out: Dict[str, Any] = {}
    for name in SERVO_NAMES:
        out[name] = {**DEFAULT_CONFIG[name], **(cfg.get(name) or {})}
    out["presets"] = {**DEFAULT_CONFIG["presets"], **(cfg.get("presets") or {})}
    return out


def _validate_pos(name: str, pos: float) -> float:
    if not 0.0 <= pos <= 1.0:
        raise ValueError(f"servo.{name}: position {pos} out of range [0.0, 1.0]")
    return float(pos)


# ─── hardware drive ────────────────────────────────────────────────────────

def _drive(name: str, pos: float) -> None:
    """Write the servo to a normalized position. Caller holds _lock."""
    s = _servos.get(name)
    if s is None:
        return
    s.angle = pos * 180.0
    minU, maxU = _pulse_us[name]
    _last_commanded_us[name] = int(minU + pos * (maxU - minU))
    _position[name] = pos


def _ensure_initialized(cfg: Dict[str, Any]) -> None:
    global _initialized, _pca
    if _initialized:
        return
    H = _hw()
    i2c = H["busio"].I2C(H["board"].SCL, H["board"].SDA)
    _pca = H["PCA9685"](i2c)
    _pca.frequency = PWM_FREQ_HZ

    Servo = H["servo_mod"].Servo
    for name in SERVO_NAMES:
        c = cfg[name]
        ch = int(c["channel"])
        minU, maxU = int(c["minUs"]), int(c["maxUs"])
        _channels[name] = ch
        _pulse_us[name] = (minU, maxU)
        _slew_rate[name] = float(c["slewRate"])
        _default_on_start[name] = _validate_pos(name, float(c["defaultOnStart"]))
        _servos[name] = Servo(_pca.channels[ch], min_pulse=minU, max_pulse=maxU)
        _position[name] = 0.0
        _target[name] = 0.0
    _initialized = True


# ─── slew loop ─────────────────────────────────────────────────────────────

def _slew_loop(db: firestore.Client, unit_id: str) -> None:
    dt = 1.0 / TICK_HZ
    next_mirror = time.monotonic() + MIRROR_PERIOD_S
    while not _stop.is_set():
        time.sleep(dt)
        with _lock:
            for name in SERVO_NAMES:
                pos = _position[name]
                tgt = _target[name]
                if pos == tgt:
                    continue
                step = _slew_rate[name] * dt
                new_pos = min(tgt, pos + step) if tgt > pos else max(tgt, pos - step)
                _drive(name, new_pos)
        now = time.monotonic()
        if now >= next_mirror:
            # Advance the gate before attempting the write, so a failing
            # mirror (offline, quota) retries on the next period rather than
            # spinning on every tick.
            next_mirror = now + MIRROR_PERIOD_S
            due = now - _last_mirror_at >= MIRROR_IDLE_PERIOD_S
            if due or _current_signature() != _last_mirrored_sig:
                try:
                    _mirror(db, unit_id, now)
                except Exception as e:
                    print(f"[servos] mirror failed: {e}", flush=True)


# ─── Firestore mirror ──────────────────────────────────────────────────────

# Value-identity of the last payload successfully written to current/servos,
# and when. The slew loop compares against these to skip no-op writes; both
# are only updated after a write actually lands, so a failed mirror is
# retried rather than silently treated as published.
_last_mirrored_sig: Optional[Tuple[Any, ...]] = None
_last_mirror_at = 0.0


def _current_signature() -> Tuple[Any, ...]:
    """Signature of what _mirror would publish right now, for change
    detection. Caller must NOT hold _lock."""
    with _lock:
        return (
            tuple(
                (
                    name,
                    round(_position[name], 4),
                    round(_target[name], 4),
                    _last_commanded_us.get(name, 0) > 0,
                )
                for name in SERVO_NAMES
            ),
            _last_command_at,
        )


def _mirror(
    db: firestore.Client, unit_id: str, now: Optional[float] = None
) -> None:
    """Snapshot live state to current/servos. Caller must NOT hold _lock.

    Unconditional by design — the command paths call this to publish a result
    immediately. Rate limiting lives in the slew loop, not here.

    `now` is the caller's monotonic reading. The slew loop passes the same
    value it used to evaluate the gate, so the gate and the timestamp it
    compares against always come from one clock read.
    """
    global _last_mirrored_sig, _last_mirror_at

    with _lock:
        snap = {
            name: {
                "position": round(_position[name], 4),
                "target":   round(_target[name], 4),
                "attached": _last_commanded_us.get(name, 0) > 0,
            }
            for name in SERVO_NAMES
        }
        last_cmd_at = _last_command_at

    # Derived from the same locked read as the payload, so the recorded
    # signature always matches exactly what went to Firestore.
    sig = (
        tuple(
            (name, snap[name]["position"], snap[name]["target"], snap[name]["attached"])
            for name in SERVO_NAMES
        ),
        last_cmd_at,
    )

    db.document(f"units/{unit_id}/current/servos").set({
        **snap,
        "lastCommandMonotonic": last_cmd_at,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }, merge=True)

    _last_mirrored_sig = sig
    _last_mirror_at = time.monotonic() if now is None else now


# ─── lifecycle ─────────────────────────────────────────────────────────────

def _ensure_slew_running(db: firestore.Client, unit_id: str) -> None:
    global _slew_thread
    if _slew_thread is not None and _slew_thread.is_alive():
        return
    _stop.clear()
    _slew_thread = threading.Thread(
        target=_slew_loop, args=(db, unit_id),
        daemon=True, name="servo-slew",
    )
    _slew_thread.start()


def start_servos(db: firestore.Client, unit_id: str) -> None:
    """Init hardware, drive to defaultOnStart, start slew thread."""
    global _last_command_at
    cfg = _load_config(db, unit_id)
    _ensure_initialized(cfg)
    with _lock:
        for name in SERVO_NAMES:
            start = _default_on_start[name]
            _target[name] = start
            _drive(name, start)
        _last_command_at = time.monotonic()
    _ensure_slew_running(db, unit_id)
    _mirror(db, unit_id)
    print(
        f"[servos] started; choke ch{_channels['choke']}, "
        f"button ch{_channels['button']}, "
        f"ac ch{_channels['ac']}",
        flush=True,
    )


def detach_all() -> None:
    """Stop slew, drive all PCA channels to 0% duty (servos go limp)."""
    global _initialized
    _stop.set()
    t = _slew_thread
    if t is not None and t.is_alive():
        t.join(timeout=1.0)
    if _pca is None:
        return
    try:
        for name in SERVO_NAMES:
            ch = _channels.get(name)
            if ch is not None:
                _pca.channels[ch].duty_cycle = 0
        _pca.deinit()
    except Exception as e:
        print(f"[servos] detach_all error: {e}", flush=True)
    _initialized = False


# ─── command handlers ──────────────────────────────────────────────────────

def handle_servo_set(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """payload = {servo: 'choke', position: 0.0-1.0}"""
    global _last_command_at
    name = payload.get("servo")
    if name not in SERVO_NAMES:
        raise ValueError(f"servo.set: invalid servo {name!r}")
    pos = _validate_pos(name, float(payload.get("position", 0.0)))

    cfg = _load_config(db, unit_id)
    _ensure_initialized(cfg)
    _ensure_slew_running(db, unit_id)

    with _lock:
        _target[name] = pos
        _last_command_at = time.monotonic()
    _mirror(db, unit_id)


def handle_servo_preset(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """payload = {name: 'idle'|'start_cold'|'start_warm'|'run'}"""
    global _last_command_at
    name = payload.get("name")
    cfg = _load_config(db, unit_id)
    preset = (cfg.get("presets") or {}).get(name)
    if preset is None:
        raise ValueError(f"servo.preset: unknown preset {name!r}")

    _ensure_initialized(cfg)
    _ensure_slew_running(db, unit_id)

    with _lock:
        for s in SERVO_NAMES:
            if s in preset:
                _target[s] = _validate_pos(s, float(preset[s]))
        _last_command_at = time.monotonic()
    _mirror(db, unit_id)


def handle_servo_update(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """payload = {configPatch: {...}}  — merges into config/servos.
    Channel changes are NOT picked up live; pulse range / slew rate /
    defaults are."""
    patch = payload.get("configPatch")
    if not isinstance(patch, dict):
        raise ValueError("servo.update: missing configPatch")
    db.document(f"units/{unit_id}/config/servos").set(patch, merge=True)
    if not _initialized:
        return
    cfg = _load_config(db, unit_id)
    with _lock:
        for s in SERVO_NAMES:
            _pulse_us[s] = (int(cfg[s]["minUs"]), int(cfg[s]["maxUs"]))
            _slew_rate[s] = float(cfg[s]["slewRate"])
            _default_on_start[s] = float(cfg[s]["defaultOnStart"])
            _servos[s].set_pulse_width_range(*_pulse_us[s])


# ─── LCD wake loop ─────────────────────────────────────────────────────────

_lcd_wake_thread: Optional[threading.Thread] = None


def _load_lcd_wake_config(db: firestore.Client, unit_id: str) -> Dict[str, Any]:
    """Read config/lcdWake, falling back to defaults if missing/unreadable."""
    base = dict(LCD_WAKE_DEFAULT_CONFIG)
    try:
        snap = db.document(f"units/{unit_id}/config/lcdWake").get()
        if snap.exists:
            base.update(snap.to_dict() or {})
    except Exception as e:
        print(f"[servos] lcdWake config read failed: {e}", flush=True)
    return base


def wake_lcd(
    db: firestore.Client,
    unit_id: str,
    press_duration_s: Optional[float] = None,
) -> None:
    """Press and release the LCD-wake button.

    Used by both the periodic loop (via `_lcd_wake_loop`) and the
    `lcd.wake` command (via `handle_lcd_wake`). Initializes the servo
    lazily so a manual wake works even if `start_servos` somehow didn't.

    `press_duration_s` is clamped to [0.05, 2.0] for safety; None falls
    back to config/lcdWake.pressDurationSec.
    """
    global _last_command_at
    cfg = _load_config(db, unit_id)
    _ensure_initialized(cfg)
    _ensure_slew_running(db, unit_id)

    if press_duration_s is None:
        press_duration_s = float(
            _load_lcd_wake_config(db, unit_id).get("pressDurationSec", 0.4)
        )
    press_duration_s = max(0.05, min(2.0, float(press_duration_s)))

    with _lock:
        _target["button"] = 1.0
        _last_command_at = time.monotonic()
    # Slew (~100 ms at default rate) + dwell so the button registers.
    time.sleep(press_duration_s)
    with _lock:
        _target["button"] = 0.0
        _last_command_at = time.monotonic()
    _mirror(db, unit_id)


def handle_lcd_wake(
    db: firestore.Client, unit_id: str, payload: Dict[str, Any]
) -> None:
    """payload = {} or {pressDurationSec: 0.4}"""
    dur = payload.get("pressDurationSec")
    wake_lcd(db, unit_id, float(dur) if dur is not None else None)


def _lcd_wake_loop(db: firestore.Client, unit_id: str) -> None:
    """Background loop that periodically presses the LCD wake button.

    Re-reads config on every iteration so toggling `enabled` or changing
    `intervalSec` in Firestore takes effect within ~10 s without a restart.
    Sleeps via `_stop.wait()` so listener shutdown kills the loop promptly.
    """
    last_wake = 0.0  # time.monotonic() of last successful press; 0 = never
    while not _stop.is_set():
        cfg = _load_lcd_wake_config(db, unit_id)
        enabled = bool(cfg.get("enabled", False))
        interval = max(60, int(cfg.get("intervalSec", 600)))

        if not enabled:
            # Idle; poll config every 10 s so flipping it on is fast.
            if _stop.wait(10):
                return
            continue

        now = time.monotonic()
        if now - last_wake >= interval:
            try:
                wake_lcd(db, unit_id, float(cfg.get("pressDurationSec", 0.4)))
                last_wake = time.monotonic()
            except Exception as e:
                print(f"[servos] lcd_wake_loop error: {e}", flush=True)
                if _stop.wait(30):
                    return
                continue

        # Sleep until the next wake is due, but in chunks so config edits
        # take effect within ~10 s even when the interval is long.
        remaining = max(1.0, interval - (time.monotonic() - last_wake))
        if _stop.wait(min(10.0, remaining)):
            return


def start_lcd_wake_loop(db: firestore.Client, unit_id: str) -> None:
    """Spawn the LCD-wake loop (idempotent). The loop respects the
    `enabled` flag in config/lcdWake — calling this when disabled just
    starts a dormant poller that activates the moment you set it true."""
    global _lcd_wake_thread
    if _lcd_wake_thread is not None and _lcd_wake_thread.is_alive():
        return
    _stop.clear()
    _lcd_wake_thread = threading.Thread(
        target=_lcd_wake_loop, args=(db, unit_id),
        daemon=True, name="lcd-wake-loop",
    )
    _lcd_wake_thread.start()
    print("[servos] lcd-wake loop started (config/lcdWake.enabled gates presses)", flush=True)


# ─── AC toggle button presser ──────────────────────────────────────────────
# Same press-and-release pattern as wake_lcd but no periodic loop — AC is
# only toggled on explicit user command. Each press flips the Predator's
# AC output state; the Pi has no read-back of which state it's in.

AC_TOGGLE_DEFAULT_PRESS_S = 0.5   # longer than LCD-wake — AC power button
                                  # needs a deliberate hold to register a
                                  # toggle, not a quick tap. Override via payload.


def press_ac(
    db: firestore.Client,
    unit_id: str,
    press_duration_s: Optional[float] = None,
) -> None:
    """Press and release the AC toggle button once.

    Each call flips the Predator's AC output state (on→off or off→on);
    no state tracking on this side. Initializes the servo lazily so a
    manual press works even if `start_servos` somehow didn't.

    `press_duration_s` is clamped to [0.05, 2.0]; None uses
    AC_TOGGLE_DEFAULT_PRESS_S.
    """
    global _last_command_at
    cfg = _load_config(db, unit_id)
    _ensure_initialized(cfg)
    _ensure_slew_running(db, unit_id)

    if press_duration_s is None:
        press_duration_s = AC_TOGGLE_DEFAULT_PRESS_S
    press_duration_s = max(0.05, min(2.0, float(press_duration_s)))

    with _lock:
        _target["ac"] = 1.0
        _last_command_at = time.monotonic()
    time.sleep(press_duration_s)
    with _lock:
        _target["ac"] = 0.0
        _last_command_at = time.monotonic()
    _mirror(db, unit_id)


def handle_ac_toggle(
    db: firestore.Client, unit_id: str, payload: Dict[str, Any]
) -> None:
    """payload = {} or {pressDurationSec: 0.25}"""
    dur = payload.get("pressDurationSec")
    press_ac(db, unit_id, float(dur) if dur is not None else None)
