"""Raspberry Pi host health — temperature, throttling, undervoltage, load.

Why this exists
───────────────
On 2026-08-04 UNIT-002 ran at 85.1 °C, thermally throttled, with a core
pinned at 100 %, for hours — and nothing anywhere surfaced it. It was found
only because someone went looking for an unrelated fault. A generator
deployed in the field has nobody to go looking.

The Pi is the one component in this system whose health was completely
unpublished. The Predator reports itself over I²C, the VESC reports itself
over CAN, and the host running both was invisible.

What it reads (all without sudo)
────────────────────────────────
  /sys/class/thermal/thermal_zone0/temp   SoC temperature, millidegrees
  /sys/class/hwmon/*/temp1_input          per-chip temps (RP1 southbridge)
  vcgencmd get_throttled                  the throttle/undervoltage bitmask
  /proc/loadavg                           1-minute load

`vcgencmd` needs membership in the `video` group, which the service user has.
If it is ever missing, every vcgencmd-derived field degrades to None rather
than raising — a publisher that dies because a diagnostic is unavailable is
worse than one that reports slightly less.

Reading the throttle bitmask
────────────────────────────
The low bits are LIVE state; bits 16-19 are sticky "has happened since boot"
and only clear on reboot. Both matter and they answer different questions:
"is it throttling right now" vs "has this unit ever been in trouble". Today
UNIT-001 showed 0x50000 — no live problem, but undervoltage HAS occurred,
which is a power-supply symptom worth knowing about and invisible from a
single spot reading.

    bit 0  under-voltage NOW          bit 16  under-voltage has occurred
    bit 1  ARM frequency capped NOW   bit 17  frequency capping has occurred
    bit 2  currently throttled        bit 18  throttling has occurred
    bit 3  soft temp limit active     bit 19  soft temp limit has occurred
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, Optional

# Pi 5 begins soft-throttling around here. Published so a consumer doesn't
# have to hardcode the same number to decide what "hot" means.
SOFT_LIMIT_C = 85.0
# Where we start calling it a problem worth telling a human about. Below the
# hard limit on purpose — the point is to warn while there is still time to
# act, not to confirm the throttle after the fact.
WARN_C = float(os.environ.get("SITEPULSE_PI_WARN_C", "75"))

_THROTTLE_BITS = (
    ("undervoltage_now",      0),
    ("freq_capped_now",       1),
    ("throttled_now",         2),
    ("soft_temp_limit_now",   3),
    ("undervoltage_since_boot", 16),
    ("freq_capped_since_boot", 17),
    ("throttled_since_boot",  18),
    ("soft_temp_limit_since_boot", 19),
)


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _vcgencmd(arg: str) -> Optional[str]:
    """Best-effort vcgencmd. Returns None if unavailable rather than raising —
    this is diagnostics, and it must never take the publisher down."""
    try:
        out = subprocess.run(
            ["vcgencmd", arg], capture_output=True, text=True, timeout=2.0
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def cpu_temp_c() -> Optional[float]:
    """SoC temperature. Read from sysfs rather than vcgencmd: no subprocess,
    no group membership required, and it is the same sensor."""
    raw = _read_int("/sys/class/thermal/thermal_zone0/temp")
    return round(raw / 1000.0, 1) if raw is not None else None


def _hwmon_temps() -> Dict[str, float]:
    """Per-chip temps keyed by hwmon name (e.g. rp1_adc). Excludes
    cpu_thermal, which is already reported as cpu_temp_c."""
    out: Dict[str, float] = {}
    base = "/sys/class/hwmon"
    try:
        entries = os.listdir(base)
    except OSError:
        return out
    for entry in entries:
        d = os.path.join(base, entry)
        try:
            with open(os.path.join(d, "name")) as fh:
                name = fh.read().strip()
        except OSError:
            continue
        if name == "cpu_thermal":
            continue
        raw = _read_int(os.path.join(d, "temp1_input"))
        if raw is not None:
            out[name] = round(raw / 1000.0, 1)
    return out


def throttle_flags() -> Dict[str, Any]:
    """Decoded `vcgencmd get_throttled`. All-None when unavailable."""
    raw = _vcgencmd("get_throttled")
    if not raw or "=" not in raw:
        return {"pi_throttled_raw": None}
    try:
        value = int(raw.split("=", 1)[1], 0)
    except ValueError:
        return {"pi_throttled_raw": None}
    flags: Dict[str, Any] = {"pi_throttled_raw": value}
    for name, bit in _THROTTLE_BITS:
        flags["pi_" + name] = bool(value & (1 << bit))
    return flags


def load_1min() -> Optional[float]:
    try:
        with open("/proc/loadavg") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def snapshot() -> Dict[str, Any]:
    """Everything, flat, ready to merge into the telemetry snapshot.

    Every value is Optional — a missing sensor publishes null rather than
    omitting the key, so a consumer can distinguish "this Pi runs an old
    publisher" (key absent) from "the sensor is unavailable" (key null).
    """
    temp = cpu_temp_c()
    out: Dict[str, Any] = {
        "pi_temp_c": temp,
        "pi_temp_warn_c": WARN_C,
        "pi_temp_soft_limit_c": SOFT_LIMIT_C,
        # Precomputed so the app and the notifier agree on "hot" without
        # each reimplementing the comparison against a threshold they'd have
        # to hardcode.
        "pi_temp_warn": (temp is not None and temp >= WARN_C),
        "pi_load_1min": load_1min(),
    }
    out.update(throttle_flags())
    for name, value in _hwmon_temps().items():
        out[f"pi_temp_{name}_c"] = value
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(snapshot(), indent=2, sort_keys=True))


# ─── overheat watcher ──────────────────────────────────────────────────────

# Sustained-crossing parameters. A generator sitting in the sun spikes; only a
# sustained rise means something. All env-tunable so a field unit can be made
# quieter without a deploy.
SUSTAIN_S    = float(os.environ.get("SITEPULSE_PI_SUSTAIN_S", "120"))
REALERT_S    = float(os.environ.get("SITEPULSE_PI_REALERT_S", "1800"))
# Clear well below the warn point, not at it — otherwise a unit hovering on the
# threshold alternates alert/clear forever.
HYSTERESIS_C = float(os.environ.get("SITEPULSE_PI_HYSTERESIS_C", "5"))


class OverheatWatcher:
    """Emits `system.overheat` / `system.overheat.cleared` events.

    Call observe() with a pi_health.snapshot() each publish cycle. State lives
    in memory only: a restart re-arms, which is the safe direction — worst case
    you get one duplicate alert on a genuinely hot unit, versus silence.

    Event shape is `{kind, at, source, payload}` deliberately. The notifier
    (functions/src/pushFanout.ts) reads `data.kind` and `data.payload`;
    engine_supervisor writes `{type, message, data}` instead and is therefore
    silently dropped by it. Do not copy that shape.
    """

    def __init__(self, db, unit_id: str) -> None:
        self._db = db
        self._unit_id = unit_id
        self._hot_since: Optional[float] = None
        self._cool_since: Optional[float] = None
        self._alerting = False
        self._last_alert_at: Optional[float] = None
        self._last_severity: Optional[str] = None

    def _severity(self, health: Dict[str, Any]) -> str:
        temp = health.get("pi_temp_c")
        throttling = bool(
            health.get("pi_throttled_now")
            or health.get("pi_soft_temp_limit_now")
            or health.get("pi_freq_capped_now")
        )
        if throttling or (temp is not None and temp >= SOFT_LIMIT_C):
            return "critical"
        return "warn"

    def _emit(self, kind: str, health: Dict[str, Any], severity: str) -> None:
        try:
            from firebase_admin import firestore as _fs
            self._db.collection(f"units/{self._unit_id}/events").add({
                "kind": kind,
                "at": _fs.SERVER_TIMESTAMP,
                "source": "pi",
                "payload": {
                    "tempC": health.get("pi_temp_c"),
                    "thresholdC": WARN_C,
                    "softLimitC": SOFT_LIMIT_C,
                    "severity": severity,
                    "throttling": bool(
                        health.get("pi_throttled_now")
                        or health.get("pi_soft_temp_limit_now")
                        or health.get("pi_freq_capped_now")
                    ),
                    "loadAvg": health.get("pi_load_1min"),
                },
            })
            print(f"[pi_health] emitted {kind} ({severity}) at "
                  f"{health.get('pi_temp_c')} C", flush=True)
        except Exception as e:
            # Never let telemetry bookkeeping take down the publisher.
            print(f"[pi_health] event emit failed for {kind}: {e!r}", flush=True)

    def observe(self, health: Dict[str, Any], now: Optional[float] = None) -> None:
        temp = health.get("pi_temp_c")
        if temp is None:
            return
        import time as _time
        now = now if now is not None else _time.monotonic()

        if temp >= WARN_C:
            self._cool_since = None
            if self._hot_since is None:
                self._hot_since = now
            severity = self._severity(health)

            if not self._alerting:
                if now - self._hot_since >= SUSTAIN_S:
                    self._alerting = True
                    self._last_alert_at = now
                    self._last_severity = severity
                    self._emit("system.overheat", health, severity)
                return

            # Already alerting. Escalate warn -> critical immediately rather
            # than waiting out the re-alert window; that transition means it is
            # actively throttling now, which is new information.
            if severity == "critical" and self._last_severity != "critical":
                self._last_alert_at = now
                self._last_severity = severity
                self._emit("system.overheat", health, severity)
            elif self._last_alert_at is not None and now - self._last_alert_at >= REALERT_S:
                self._last_alert_at = now
                self._emit("system.overheat", health, severity)
            return

        # Below the warn point.
        self._hot_since = None
        if not self._alerting:
            return
        if temp > WARN_C - HYSTERESIS_C:
            # In the hysteresis band — neither hot nor recovered. Hold.
            self._cool_since = None
            return
        if self._cool_since is None:
            self._cool_since = now
            return
        if now - self._cool_since >= SUSTAIN_S:
            self._alerting = False
            self._last_alert_at = None
            self._last_severity = None
            self._cool_since = None
            self._emit("system.overheat.cleared", health, "info")
