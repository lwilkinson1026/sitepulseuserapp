"""
PCA9685 (V1246) servo controller for choke + throttle.

Modeled on pi/relays.py:
- Lazy hardware import so this module loads on a dev Mac.
- Module state under a single lock.
- Exposed handlers wired into command_listener.HANDLERS.

Conventions (from feature_plan_servo.md):
- Positions are normalized 0.0-1.0. Per-servo pulse-width range
  (minUs/maxUs) maps the normalized value to actual PWM via the
  adafruit_motor.servo.Servo class (angle = pos * 180).
- Choke:    0.0 = open/run,    1.0 = closed/cold-start.
- Throttle: 0.0 = idle,        1.0 = wide open.
- Mounting orientation determines polarity; if a linkage gets flipped,
  swap minUs/maxUs in config rather than negating positions in code.

Exposed handlers (imported lazily by command_listener.py):
    handle_servo_set(db, unit_id, payload)
    handle_servo_preset(db, unit_id, payload)
    handle_servo_update(db, unit_id, payload)

Lifecycle:
    start_servos(db, unit_id) — boot: drive defaults, start slew loop.
    detach_all()              — shutdown: cut PWM so servos go limp.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

from firebase_admin import firestore


SERVO_NAMES = ("choke", "throttle")
PWM_FREQ_HZ = 50          # standard hobby servo
TICK_HZ = 50              # slew loop tick rate
MIRROR_PERIOD_S = 1.0     # heartbeat for current/servos


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
_default_on_fault: Dict[str, float] = {}
_position: Dict[str, float] = {}
_target: Dict[str, float] = {}
_last_commanded_us: Dict[str, int] = {}
_last_command_at: float = 0.0
_watchdog_enabled: bool = False
_watchdog_timeout_s: float = 5.0
_watchdog_armed: bool = False
_slew_thread: Optional[threading.Thread] = None
_stop = threading.Event()


# ─── config loader ─────────────────────────────────────────────────────────

# Calibrated 2026-05-18 on the bench: usable pulse range ~1333-1667 µs
# for both servos with no linkage attached. Polarity resolves once the
# linkages mount; for now leave both defaults at "safe / engine-off".
DEFAULT_CONFIG: Dict[str, Any] = {
    "choke":    {"channel": 0, "minUs": 1333, "maxUs": 1667, "slewRate": 2.0,
                 "defaultOnStart": 0.0, "defaultOnFault": 0.0},
    "throttle": {"channel": 1, "minUs": 1333, "maxUs": 1667, "slewRate": 1.5,
                 "defaultOnStart": 0.0, "defaultOnFault": 0.0},
    "presets": {
        "idle":       {"choke": 0.0, "throttle": 0.0},
        "start_cold": {"choke": 1.0, "throttle": 0.2},
        "start_warm": {"choke": 0.3, "throttle": 0.15},
        "run":        {"choke": 0.0, "throttle": 0.5},
    },
    # v1: time-based only, no engine-state check. Default off so the
    # bench doesn't slam throttle to 0 every 5s. Engine-state-aware v2
    # waits on a working VESC CAN frame.
    "watchdogEnabled": False,
    "watchdogTimeoutS": 5,
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
    out["watchdogEnabled"] = bool(cfg.get("watchdogEnabled", DEFAULT_CONFIG["watchdogEnabled"]))
    out["watchdogTimeoutS"] = float(cfg.get("watchdogTimeoutS", DEFAULT_CONFIG["watchdogTimeoutS"]))
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
        _default_on_fault[name] = _validate_pos(name, float(c["defaultOnFault"]))
        _servos[name] = Servo(_pca.channels[ch], min_pulse=minU, max_pulse=maxU)
        _position[name] = 0.0
        _target[name] = 0.0
    _initialized = True


# ─── slew loop ─────────────────────────────────────────────────────────────

def _slew_loop(db: firestore.Client, unit_id: str) -> None:
    global _watchdog_armed
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
            if _watchdog_enabled and _last_command_at > 0:
                since = time.monotonic() - _last_command_at
                if since > _watchdog_timeout_s and not _watchdog_armed:
                    _target["throttle"] = _default_on_fault["throttle"]
                    _watchdog_armed = True
                    print(
                        f"[servos] watchdog: no command for {since:.1f}s, "
                        f"throttle -> {_default_on_fault['throttle']}",
                        flush=True,
                    )
        now = time.monotonic()
        if now >= next_mirror:
            try:
                _mirror(db, unit_id)
            except Exception as e:
                print(f"[servos] mirror failed: {e}", flush=True)
            next_mirror = now + MIRROR_PERIOD_S


# ─── Firestore mirror ──────────────────────────────────────────────────────

def _mirror(db: firestore.Client, unit_id: str) -> None:
    """Snapshot live state to current/servos. Caller must NOT hold _lock."""
    with _lock:
        snap = {
            name: {
                "position": round(_position[name], 4),
                "target":   round(_target[name], 4),
                "attached": _last_commanded_us.get(name, 0) > 0,
            }
            for name in SERVO_NAMES
        }
        watchdog_armed = _watchdog_armed
        last_cmd_at = _last_command_at
    db.document(f"units/{unit_id}/current/servos").set({
        **snap,
        "watchdogArmed": watchdog_armed,
        "lastCommandMonotonic": last_cmd_at,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }, merge=True)


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
    global _watchdog_enabled, _watchdog_timeout_s, _last_command_at
    cfg = _load_config(db, unit_id)
    _ensure_initialized(cfg)
    _watchdog_enabled = bool(cfg["watchdogEnabled"])
    _watchdog_timeout_s = float(cfg["watchdogTimeoutS"])
    with _lock:
        for name in SERVO_NAMES:
            start = _default_on_start[name]
            _target[name] = start
            # Drive immediately — the previous run may have crashed mid-
            # motion, don't assume the servo is where we left it.
            _drive(name, start)
        _last_command_at = time.monotonic()
    _ensure_slew_running(db, unit_id)
    _mirror(db, unit_id)
    print(
        f"[servos] started; choke ch{_channels['choke']} "
        f"throttle ch{_channels['throttle']} "
        f"watchdog={'on' if _watchdog_enabled else 'off'}",
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
    """payload = {servo: 'choke'|'throttle', position: 0.0-1.0}"""
    global _last_command_at, _watchdog_armed
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
        _watchdog_armed = False
    _mirror(db, unit_id)


def handle_servo_preset(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """payload = {name: 'idle'|'start_cold'|'start_warm'|'run'}"""
    global _last_command_at, _watchdog_armed
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
        _watchdog_armed = False
    _mirror(db, unit_id)


def handle_servo_update(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """payload = {configPatch: {...}}  — merges into config/servos.
    Channel changes are NOT picked up live; pulse range / slew rate /
    defaults / watchdog flags are."""
    global _watchdog_enabled, _watchdog_timeout_s
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
            _default_on_fault[s] = float(cfg[s]["defaultOnFault"])
            _servos[s].set_pulse_width_range(*_pulse_us[s])
        _watchdog_enabled = bool(cfg["watchdogEnabled"])
        _watchdog_timeout_s = float(cfg["watchdogTimeoutS"])
