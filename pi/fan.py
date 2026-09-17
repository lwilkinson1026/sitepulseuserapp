"""
Engine-bay cooling fan speed control (4-wire PWM fan).

UNIT-001's fan is a Delta AFB1548VH, 48 V / 0.90 A, with a PWM speed input
and a tach output. Left alone its control lead floats high and the fan runs
flat out — about 43 W and far louder than it needs to be. This module drives
that lead so the fan follows what the engine is doing.

Power is NOT switched here. The 48 V feed still goes through the fan relay
(relays.py, engine-follow), exactly as on the 2-wire fan it replaces. This
only sets how hard the fan works while it has power.

Wiring (one small NPN, e.g. 2N3904 / PN2222A):

    GPIO 12 (pin 32) ──1 kΩ──► base          collector ──► fan control lead
    base ──10 kΩ──► emitter                  emitter   ──► Pi GND (pin 34)

The transistor is there because the fan pulls its control lead up internally
to as much as 5 V and Pi pins are 3.3 V only: the Pi just sinks the lead to
ground and never sees the fan's voltage. It also inverts — GPIO high pulls
the lead low — which `SITEPULSE_FAN_PWM_INVERT=1` (the default) accounts for.
The 10 kΩ holds the transistor off while the Pi boots.

Fail-safe: anything that stops this module — crash, shutdown, a pin that
never gets claimed — leaves the transistor off, the lead floating, and the
fan at FULL speed. It can fail loud; it cannot fail hot.

Backends, picked automatically:
  hardware  sysfs PWM at 25 kHz. Needs `dtoverlay=pwm,pin=12,func=4` in
            /boot/firmware/config.txt and a reboot. Preferred.
  software  lgpio tx_pwm at 1 kHz. Works with no boot change. The fan reads
            duty cycle, not power, so the lower frequency is acceptable.

Config, `units/{u}/config/fan` (all optional):
    mode       "auto" (follow engine state) | "manual"
    manualPct  0-100, used in manual mode
    speeds     {engine_state: pct, "default": pct}
    minPct     floor applied to any non-zero request (many fans stall low)

Exposed:
    start_fan(db, unit_id)                  boot: claim the pin, apply speed
    reconcile(db, unit_id, engine_state)    called from engine._publish_state
    handle_fan_set(db, unit_id, payload)    command `fan.set`
    release()                               shutdown: let go → full speed
"""

from __future__ import annotations

import glob
import os
import threading
import time
from typing import Any, Dict, Optional

from firebase_admin import firestore


PIN          = int(os.environ.get("SITEPULSE_FAN_PWM_PIN", "12"))
INVERT       = os.environ.get("SITEPULSE_FAN_PWM_INVERT", "1") not in ("0", "", "false")
HW_HZ        = int(os.environ.get("SITEPULSE_FAN_PWM_HZ", "25000"))
SW_HZ        = int(os.environ.get("SITEPULSE_FAN_PWM_SW_HZ", "1000"))
HW_CHANNEL   = int(os.environ.get("SITEPULSE_FAN_PWM_CHANNEL", "0"))   # GPIO 12 = PWM0_CHAN0
HW_CHIP      = os.environ.get("SITEPULSE_FAN_PWM_CHIP", "")            # e.g. /sys/class/pwm/pwmchip0
GPIO_CHIP    = 0

DEFAULT_CONFIG: Dict[str, Any] = {
    "mode": "auto",
    "manualPct": 60,
    # Percent of full speed per engine state. The relay removes 48 V whenever
    # the engine is not running, so the idle figure only matters if someone
    # forces the fan relay on.
    "speeds": {
        "starting": 50,
        "cranking": 50,
        "running":  60,
        "charging": 80,   # regen loads the engine hardest
        "stopping": 60,
        "default":  40,
    },
    "minPct": 20,
}

_lock = threading.Lock()
_backend: Optional[str] = None          # "hardware" | "software"
_hw_dir: Optional[str] = None
_lgpio: Any = None
_h: Optional[int] = None
_applied_pct: Optional[int] = None
_last_mirror: Optional[tuple] = None
_sw_running = False                     # is lgpio's PWM generator active on PIN


def _sw_stop() -> None:
    """Stop lgpio's software PWM if it is running. lgpio raises 'bad PWM
    micros' when asked to stop a generator that is not running, so track it."""
    global _sw_running
    if _sw_running:
        try:
            _lgpio.tx_pwm(_h, PIN, 0, 0)
        except Exception as e:
            print(f"[fan] stopping software PWM: {e}", flush=True)
        _sw_running = False


# ─── config ────────────────────────────────────────────────────────────────

def _load_config(db: firestore.Client, unit_id: str) -> Dict[str, Any]:
    cfg = {**DEFAULT_CONFIG, "speeds": dict(DEFAULT_CONFIG["speeds"])}
    try:
        snap = db.document(f"units/{unit_id}/config/fan").get()
        if snap.exists:
            doc = snap.to_dict() or {}
            cfg["speeds"].update(doc.get("speeds") or {})
            for k in ("mode", "manualPct", "minPct"):
                if k in doc:
                    cfg[k] = doc[k]
    except Exception as e:
        print(f"[fan] config read failed, using defaults: {e}", flush=True)
    return cfg


def _target_pct(cfg: Dict[str, Any], engine_state: Optional[str]) -> int:
    if cfg.get("mode") == "manual":
        pct = float(cfg.get("manualPct", 60))
    else:
        speeds = cfg["speeds"]
        pct = float(speeds.get(engine_state or "", speeds.get("default", 40)))
    pct = max(0.0, min(100.0, pct))
    if pct > 0:
        pct = max(pct, float(cfg.get("minPct", 20)))
    return int(round(pct))


# ─── backends ──────────────────────────────────────────────────────────────

def _find_hw_chip() -> Optional[str]:
    if HW_CHIP:
        return HW_CHIP if os.path.isdir(HW_CHIP) else None
    for chip in sorted(glob.glob("/sys/class/pwm/pwmchip*")):
        try:
            if int(open(f"{chip}/npwm").read()) > HW_CHANNEL:
                return chip
        except Exception:
            continue
    return None


def _write(path: str, value: Any, tries: int = 20) -> None:
    """sysfs write with retry: after `export`, udev needs a moment to hand the
    new pwmN files to the gpio group."""
    last: Optional[Exception] = None
    for _ in range(tries):
        try:
            with open(path, "w") as f:
                f.write(str(value))
            return
        except (PermissionError, FileNotFoundError) as e:
            last = e
            time.sleep(0.05)
    raise last  # type: ignore[misc]


def _init_hardware(chip: str) -> str:
    d = f"{chip}/pwm{HW_CHANNEL}"
    if not os.path.isdir(d):
        _write(f"{chip}/export", HW_CHANNEL)
    period = int(1e9 / HW_HZ)
    try:
        _write(f"{d}/duty_cycle", 0)
    except OSError:
        pass                      # unset period rejects any duty; harmless
    _write(f"{d}/period", period)
    _write(f"{d}/duty_cycle", 0)
    _write(f"{d}/enable", 1)
    return d


def _ensure_initialized() -> None:
    global _backend, _hw_dir, _lgpio, _h
    if _backend is not None:
        return
    chip = _find_hw_chip()
    if chip:
        try:
            _hw_dir = _init_hardware(chip)
            _backend = "hardware"
            return
        except Exception as e:
            print(f"[fan] hardware PWM at {chip} unusable ({e}); falling back to software", flush=True)
    import lgpio  # type: ignore[import-not-found]
    _lgpio = lgpio
    _h = lgpio.gpiochip_open(GPIO_CHIP)
    rc = lgpio.gpio_claim_output(_h, PIN, 0)      # low = transistor off = full speed
    if rc < 0:
        lgpio.gpiochip_close(_h)
        _h = None
        raise RuntimeError(f"fan: claim GPIO{PIN} failed rc={rc}")
    _backend = "software"


def _apply(pct: int) -> None:
    """Drive the control lead for `pct` percent of full speed. Caller holds _lock."""
    global _applied_pct, _sw_running
    _ensure_initialized()
    # The fan wants `pct` duty on its lead. Through the inverting NPN that is
    # (100 - pct) duty on the GPIO.
    gpio_duty = (100 - pct) if INVERT else pct
    if _backend == "hardware":
        period = int(1e9 / HW_HZ)
        _write(f"{_hw_dir}/duty_cycle", int(period * gpio_duty / 100))
    else:
        if gpio_duty <= 0 or gpio_duty >= 100:
            _sw_stop()                                       # a steady level, no generator
            _lgpio.gpio_write(_h, PIN, 1 if gpio_duty >= 100 else 0)
        else:
            _lgpio.tx_pwm(_h, PIN, SW_HZ, gpio_duty)
            _sw_running = True
    _applied_pct = pct


def _mirror(db: firestore.Client, unit_id: str, cfg: Dict[str, Any], state: Optional[str]) -> None:
    """Publish only on change — this runs on every engine state transition."""
    global _last_mirror
    sig = (_applied_pct, cfg.get("mode"), _backend)
    if sig == _last_mirror:
        return
    try:
        db.document(f"units/{unit_id}/current/fan").set({
            "speedPct":    _applied_pct,
            "mode":        cfg.get("mode"),
            "backend":     _backend,
            "pwmHz":       HW_HZ if _backend == "hardware" else SW_HZ,
            "pin":         PIN,
            "engineState": state,
            "updatedAt":   firestore.SERVER_TIMESTAMP,
        }, merge=True)
        _last_mirror = sig
    except Exception as e:
        print(f"[fan] mirror failed: {e}", flush=True)


# ─── public API ────────────────────────────────────────────────────────────

def reconcile(db: firestore.Client, unit_id: str, engine_state: Optional[str] = None) -> None:
    """Set the fan speed for the current engine state. Best-effort by design:
    the caller is engine._publish_state and must never be blocked by a fan."""
    cfg = _load_config(db, unit_id)
    pct = _target_pct(cfg, engine_state)
    with _lock:
        if pct != _applied_pct:
            _apply(pct)
            print(f"[fan] {pct}% ({cfg.get('mode')}, engine={engine_state}, {_backend})", flush=True)
    _mirror(db, unit_id, cfg, engine_state)


def start_fan(db: firestore.Client, unit_id: str) -> None:
    state: Optional[str] = None
    try:
        state = (db.document(f"units/{unit_id}/current/engine").get().to_dict() or {}).get("state")
    except Exception:
        pass
    reconcile(db, unit_id, state)
    print(
        f"[fan] started; GPIO{PIN} {_backend} PWM "
        f"{HW_HZ if _backend == 'hardware' else SW_HZ} Hz, invert={INVERT}",
        flush=True,
    )


def handle_fan_set(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """payload = {mode: 'auto'} or {mode: 'manual', speedPct: 0-100}"""
    mode = payload.get("mode")
    if mode not in ("auto", "manual"):
        raise ValueError(f"fan.set: invalid mode {mode!r}")
    patch: Dict[str, Any] = {"mode": mode}
    if mode == "manual":
        pct = float(payload.get("speedPct", DEFAULT_CONFIG["manualPct"]))
        if not 0 <= pct <= 100:
            raise ValueError(f"fan.set: speedPct {pct} out of range [0, 100]")
        patch["manualPct"] = pct
    db.document(f"units/{unit_id}/config/fan").set(patch, merge=True)
    state = (db.document(f"units/{unit_id}/current/engine").get().to_dict() or {}).get("state")
    reconcile(db, unit_id, state)


def release() -> None:
    """Let go of the control lead. The fan returns to full speed."""
    global _backend, _h, _applied_pct
    with _lock:
        try:
            if _backend == "hardware" and _hw_dir:
                _write(f"{_hw_dir}/duty_cycle", 0 if INVERT else int(1e9 / HW_HZ))
            elif _backend == "software" and _h is not None:
                _sw_stop()
                _lgpio.gpio_write(_h, PIN, 0 if INVERT else 1)
                _lgpio.gpio_free(_h, PIN)
                _lgpio.gpiochip_close(_h)
        except Exception as e:
            print(f"[fan] release error: {e}", flush=True)
        _backend, _h, _applied_pct = None, None, None
