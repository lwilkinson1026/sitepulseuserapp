"""
VESC6 CAN telemetry listener.

Background thread that reads socketcan frames from can0, decodes the five
status messages broadcast automatically by the controller, and maintains the
latest values in a thread-safe dict so the publisher can pull them into
snapshots at its own cadence (no need to publish at the controller's 50 Hz).

VESC6 CAN format
----------------
- Extended 29-bit IDs only.
- Lower 8 bits  = controller unit ID (0-255).
- Bits 8-15     = command number.
- Status messages are sent unsolicited at a rate configurable in VESC Tool.

Status payload layouts (all big-endian; offsets in bytes):

  cmd 9  STATUS_1  RPM (i32, 0-3)              · Total Current ×10 (i16, 4-5) · Duty ×1000 (i16, 6-7)
  cmd 14 STATUS_2  Amp Hours ×10000 (i32, 0-3) · Amp Hours Charged ×10000 (i32, 4-7)
  cmd 15 STATUS_3  Watt Hours ×10000 (i32, 0-3) · Watt Hours Charged ×10000 (i32, 4-7)
  cmd 16 STATUS_4  FET Temp ×10 (i16, 0-1)     · Motor Temp ×10 (i16, 2-3)
                   Total Current In ×10 (i16, 4-5) · PID Pos ×50 (i16, 6-7)
  cmd 27 STATUS_5  Tachometer (i32, 0-3)        · Input Voltage ×10 (i16, 4-5) · reserved (i16, 6-7)

The PDF labels "data 0" as the rightmost cell of its table — that's the
*field* layout, not the wire order. On the wire, byte 0 (data[0]) is the
high byte of the leftmost field, matching VESC firmware's big-endian pack.
If telemetry ever comes out as nonsense (e.g. voltage near 0 or RPM at
2 billion), suspect that and flip endianness in `_i32_be` / `_i16_be`.

Setup on the Pi
---------------
    sudo apt install -y python3-can
    sudo systemctl enable --now sitepulse-can.service   # brings can0 up @ 500k

Env overrides
-------------
    SITEPULSE_VESC_IFACE      socketcan interface, default 'can0'
    SITEPULSE_VESC_UNIT_ID    controller unit ID to filter for, default 0
    SITEPULSE_VESC_STALE_SEC  freshness window for motor_stale, default 2.0
    SITEPULSE_VESC_DEBUG      '1' to log every received frame
"""

from __future__ import annotations

import os
import struct
import threading
import time
from typing import Any, Dict, Optional


# ─── command numbers (top byte of the extended ID) ────────────────────────

CMD_STATUS_1 = 9
CMD_STATUS_2 = 14
CMD_STATUS_3 = 15
CMD_STATUS_4 = 16
CMD_STATUS_5 = 27

_STATUS_CMDS = frozenset(
    {CMD_STATUS_1, CMD_STATUS_2, CMD_STATUS_3, CMD_STATUS_4, CMD_STATUS_5}
)


def _i32_be(buf: bytes, off: int) -> int:
    return struct.unpack(">i", buf[off:off + 4])[0]


def _i16_be(buf: bytes, off: int) -> int:
    return struct.unpack(">h", buf[off:off + 2])[0]


# ─── env ───────────────────────────────────────────────────────────────────

VESC_IFACE     = os.environ.get("SITEPULSE_VESC_IFACE", "can0")
VESC_UNIT_ID   = int(os.environ.get("SITEPULSE_VESC_UNIT_ID", "0"))
VESC_STALE_SEC = float(os.environ.get("SITEPULSE_VESC_STALE_SEC", "2.0"))
VESC_DEBUG     = os.environ.get("SITEPULSE_VESC_DEBUG") == "1"


class VescListener:
    """Background CAN listener that decodes VESC STATUS 1-5 frames."""

    def __init__(
        self,
        iface: str = VESC_IFACE,
        unit_id: int = VESC_UNIT_ID,
        stale_sec: float = VESC_STALE_SEC,
        debug: bool = VESC_DEBUG,
    ) -> None:
        self.iface = iface
        self.unit_id = unit_id
        self.stale_sec = stale_sec
        self.debug = debug
        self._lock = threading.Lock()
        self._values: Dict[str, Any] = {}
        self._last_update_at: float = 0.0  # monotonic; 0.0 means never
        self._frame_counts: Dict[int, int] = {c: 0 for c in _STATUS_CMDS}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background CAN listener thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="vesc-listener"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ── snapshot for the publisher ────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """Return a copy of the latest values + freshness indicator.

        Always includes `motor_stale` (bool). If no frame has ever been
        received, `motor_stale=True` and no `motor_*` value keys are
        populated.
        """
        with self._lock:
            snap = dict(self._values)
            last = self._last_update_at

        if last == 0.0:
            snap["motor_stale"] = True
        else:
            age = time.monotonic() - last
            snap["motor_stale"] = age > self.stale_sec
            snap["motor_age_sec"] = round(age, 2)
        return snap

    def frame_counts(self) -> Dict[int, int]:
        """Per-status-message frame counts since process start. Diagnostic."""
        with self._lock:
            return dict(self._frame_counts)

    # ── decode loop ───────────────────────────────────────────────────────

    def _loop(self) -> None:
        try:
            import can  # type: ignore[import-not-found]
        except ImportError:
            print(
                "[vesc] python-can not installed; listener disabled "
                "(run `sudo apt install python3-can`)",
                flush=True,
            )
            return

        # Outer loop: keep trying to (re)open can0 forever, so a boot race
        # against sitepulse-can.service or a runtime cable drop doesn't
        # silently kill the thread.
        while not self._stop.is_set():
            bus = self._open_bus_with_retry(can)
            if bus is None:
                return  # stop() was called while we were waiting
            print(
                f"[vesc] listening on {self.iface}, filtering unit_id={self.unit_id}",
                flush=True,
            )
            try:
                while not self._stop.is_set():
                    msg = bus.recv(timeout=1.0)
                    if msg is None:
                        continue
                    if not msg.is_extended_id:
                        continue
                    cmd = (msg.arbitration_id >> 8) & 0xFF
                    unit = msg.arbitration_id & 0xFF
                    if unit != self.unit_id:
                        continue
                    if cmd not in _STATUS_CMDS:
                        continue
                    if self.debug:
                        print(
                            f"[vesc] cmd={cmd} unit={unit} data={msg.data.hex()}",
                            flush=True,
                        )
                    self._handle_frame(cmd, bytes(msg.data))
            except Exception as e:
                print(
                    f"[vesc] recv error: {e!r} — closing bus and retrying open",
                    flush=True,
                )
            finally:
                try:
                    bus.shutdown()
                except Exception:
                    pass
        print("[vesc] listener stopped", flush=True)

    def _open_bus_with_retry(self, can_mod: Any) -> Any:
        """Block until can0 opens or stop() is called. Returns the bus, or
        None if we were asked to stop while waiting."""
        delay = 2.0
        while not self._stop.is_set():
            try:
                return can_mod.Bus(channel=self.iface, interface="socketcan")
            except Exception as e:
                print(
                    f"[vesc] cannot open {self.iface}: {e!r} "
                    f"(is sitepulse-can.service running?) — retrying in {delay:.0f}s",
                    flush=True,
                )
            # Wait on the stop event so .stop() wakes us immediately.
            if self._stop.wait(timeout=delay):
                return None
            delay = min(delay * 2, 30.0)
        return None

    def _handle_frame(self, cmd: int, data: bytes) -> None:
        if len(data) < 8:
            return
        with self._lock:
            self._frame_counts[cmd] = self._frame_counts.get(cmd, 0) + 1

            if cmd == CMD_STATUS_1:
                self._values["motor_rpm"] = _i32_be(data, 0)
                self._values["motor_amps"] = _i16_be(data, 4) / 10.0
                self._values["motor_duty"] = _i16_be(data, 6) / 1000.0
            elif cmd == CMD_STATUS_2:
                self._values["motor_ah"] = _i32_be(data, 0) / 10000.0
                self._values["motor_ah_charged"] = _i32_be(data, 4) / 10000.0
            elif cmd == CMD_STATUS_3:
                self._values["motor_wh"] = _i32_be(data, 0) / 10000.0
                self._values["motor_wh_charged"] = _i32_be(data, 4) / 10000.0
            elif cmd == CMD_STATUS_4:
                self._values["motor_fet_temp_c"] = _i16_be(data, 0) / 10.0
                self._values["motor_temp_c"] = _i16_be(data, 2) / 10.0
                self._values["motor_amps_in"] = _i16_be(data, 4) / 10.0
                # PID Pos × 50 is at bytes 6-7 — unit is unclear (encoder
                # ticks? degrees?), skip until we have a use for it.
            elif cmd == CMD_STATUS_5:
                self._values["motor_tachometer"] = _i32_be(data, 0)
                self._values["motor_volts"] = _i16_be(data, 4) / 10.0

            self._last_update_at = time.monotonic()


# ─── CLI smoke test ───────────────────────────────────────────────────────
# Run directly to verify decoding without involving Firestore:
#     python3 -u pi/vesc_listener.py
# Watches can0 for ~30 s and prints decoded snapshots every second.

if __name__ == "__main__":
    listener = VescListener(debug=VESC_DEBUG)
    listener.start()
    try:
        for _ in range(30):
            time.sleep(1.0)
            snap = listener.snapshot()
            counts = listener.frame_counts()
            print(f"[smoke] counts={counts}  snap={snap}", flush=True)
    finally:
        listener.stop()
