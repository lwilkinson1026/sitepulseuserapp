"""
VESC6 CAN command sender.

Constructs and transmits the write commands defined in the VESC6 CAN
format reference (see docs/VESC6_CAN_CommandsTelemetry.pdf). Inverse of
pi/vesc_listener.py — that reads STATUS frames, this writes SET_* frames.

Frame layout (matches VESC firmware big-endian convention):
    Extended 29-bit ID = (command_number << 8) | unit_id
    Payload            = int32 big-endian of (value × scale_factor)

Scale factors per the PDF:
    SET_DUTY              cmd 0   duty × 100000
    SET_CURRENT           cmd 1   amps × 1000        (i.e. milliamps)
    SET_CURRENT_BRAKE     cmd 2   amps × 1000
    SET_RPM               cmd 3   rpm × 1            (no scaling)
    SET_CURRENT_REL       cmd 10  ratio × 100000
    SET_CURRENT_BRAKE_REL cmd 11  ratio × 100000

Each sender holds its own python-can Bus. socketcan supports multiple
opens on the same interface, so a sender can coexist with the listener
(VescListener) without locking or sharing state.

VESC command-timeout watchdog
-----------------------------
VESC firmware cuts the motor if no SET_* command has been received within
~1 second (configurable in App Configuration → General → CAN Command Timeout).
To hold any duty / current demand for longer than that, the caller MUST
repeat the command at ≥ 5 Hz. See pi/engine.py for the cranking loop.

This is a *feature*: if the Pi crashes or the wire is cut, the motor stops.
Do not try to defeat it.
"""

from __future__ import annotations

import os
import struct
from typing import Any, Optional


# Command numbers
CAN_PACKET_SET_DUTY              = 0
CAN_PACKET_SET_CURRENT           = 1
CAN_PACKET_SET_CURRENT_BRAKE     = 2
CAN_PACKET_SET_RPM               = 3
CAN_PACKET_SET_CURRENT_REL       = 10
CAN_PACKET_SET_CURRENT_BRAKE_REL = 11


# ─── env ───────────────────────────────────────────────────────────────────

VESC_IFACE   = os.environ.get("SITEPULSE_VESC_IFACE", "can0")
VESC_UNIT_ID = int(os.environ.get("SITEPULSE_VESC_UNIT_ID", "0"))


class VescSender:
    """Thin wrapper around python-can that constructs VESC SET_* frames.

    Open with a context manager so the bus shuts down cleanly:

        with VescSender(unit_id=100) as v:
            v.set_current(65.0)
    """

    def __init__(
        self,
        iface: str = VESC_IFACE,
        unit_id: int = VESC_UNIT_ID,
    ) -> None:
        self.iface = iface
        self.unit_id = unit_id
        self._bus: Optional[Any] = None  # python-can Bus

    # ── lifecycle ─────────────────────────────────────────────────────────

    def open(self) -> None:
        if self._bus is not None:
            return
        import can  # type: ignore[import-not-found]
        self._bus = can.Bus(channel=self.iface, interface="socketcan")

    def close(self) -> None:
        if self._bus is None:
            return
        try:
            self._bus.shutdown()
        except Exception:
            pass
        self._bus = None

    def __enter__(self) -> "VescSender":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── low-level send ────────────────────────────────────────────────────

    def _send(self, cmd: int, value: int) -> None:
        if self._bus is None:
            raise RuntimeError("VescSender: bus not open (call open() first)")
        import can  # type: ignore[import-not-found]
        arb_id = (cmd << 8) | (self.unit_id & 0xFF)
        payload = struct.pack(">i", int(value))
        msg = can.Message(
            arbitration_id=arb_id,
            data=payload,
            is_extended_id=True,
        )
        self._bus.send(msg)

    # ── public command API ────────────────────────────────────────────────

    def set_duty(self, duty: float) -> None:
        """Demand a duty cycle from -1.0 to 1.0. Direct PWM modulation."""
        duty = max(-1.0, min(1.0, float(duty)))
        self._send(CAN_PACKET_SET_DUTY, round(duty * 100000))

    def set_current(self, amps: float) -> None:
        """Demand a motor current in amps. Positive = drive, negative = regen.
        VESC will clamp to its configured motor-current limits."""
        amps = max(-1e6, min(1e6, float(amps)))  # int32 / 1000 = max ±2.1e6
        self._send(CAN_PACKET_SET_CURRENT, round(amps * 1000))

    def set_current_brake(self, amps: float) -> None:
        """Demand a brake current in amps (always >= 0)."""
        amps = max(0.0, min(1e6, float(amps)))
        self._send(CAN_PACKET_SET_CURRENT_BRAKE, round(amps * 1000))

    def set_rpm(self, rpm: int) -> None:
        """Demand a motor RPM (closed-loop). Sign = direction."""
        rpm = max(-2**31, min(2**31 - 1, int(rpm)))
        self._send(CAN_PACKET_SET_RPM, rpm)

    def set_current_rel(self, ratio: float) -> None:
        """Demand a relative current, -1.0 to 1.0, as a fraction of the
        configured motor-current limit."""
        ratio = max(-1.0, min(1.0, float(ratio)))
        self._send(CAN_PACKET_SET_CURRENT_REL, round(ratio * 100000))

    def set_current_brake_rel(self, ratio: float) -> None:
        """Relative brake current, 0.0 to 1.0."""
        ratio = max(0.0, min(1.0, float(ratio)))
        self._send(CAN_PACKET_SET_CURRENT_BRAKE_REL, round(ratio * 100000))

    def release(self) -> None:
        """Send SET_CURRENT(0) — explicit "stop driving" command.

        Often optional because the VESC's watchdog cuts the motor ~1s
        after the last command anyway, but issuing this makes the stop
        immediate (e.g., on engine catch we want to disengage NOW, not
        in up to 1 second)."""
        self.set_current(0.0)
