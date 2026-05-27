"""
Passive I²C sniffer for the Predator 2000W BMS↔LCD bus.

The Pi cannot natively be a passive I²C listener (its hardware I²C
peripheral is a master controller), so we bit-bang sniff by configuring
SCL and SDA as plain GPIO inputs and tracking every edge on both lines.
At the Predator's ~9 kHz bus speed and the Pi 5's 2.4 GHz CPU there are
~130 000 cycles between edges — comfortably easy.

We use the `lgpio` library because `pigpio` is not viable on the Pi 5.
Edge callbacks fire on a background daemon thread inside lgpio; we
push completed transactions into a thread-safe queue that the publisher
drains in its main loop.

Wiring (Pi 5):
    GPIO 22 (pin 15) — SDA, splice to the LCD's white wire
    GPIO 23 (pin 16) — SCL, splice to the LCD's yellow wire
    GND     (pin 6)  — splice to the LCD's green wire
    DO NOT connect any Pi power rail to the Predator harness.

We deliberately avoid GPIO 2/3 (the kernel's i2c-1 bus) because the PCA9685
servo driver already lives there.  Two I²C masters on the same wires would
collide.  Since we bit-bang via lgpio edge interrupts rather than the
kernel I²C peripheral, we're free to pick any unused GPIO pair.

The bus is already pulled up by the LCD harness itself, so we do NOT add
internal pull-ups — that would double-load the bus.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import lgpio  # type: ignore[import-not-found]
except ImportError:
    lgpio = None  # graceful fallback for off-Pi development / unit tests


SDA_PIN          = int(os.environ.get("SITEPULSE_PREDATOR_SDA_PIN", "22"))
SCL_PIN          = int(os.environ.get("SITEPULSE_PREDATOR_SCL_PIN", "23"))
GPIO_CHIP        = int(os.environ.get("SITEPULSE_PREDATOR_GPIO_CHIP", "0"))
WATCH_ADDRESS    = int(os.environ.get("SITEPULSE_PREDATOR_ADDR", "0x3E"), 0)


# ─── data types ────────────────────────────────────────────────────────────

@dataclass
class Transaction:
    """A single completed I²C transaction (START → ADDR → data → STOP)."""
    address: int            # 7-bit address
    is_read: bool           # True if R/W bit was 1
    ack: bool               # True if slave ACKed the address byte
    data: list[int] = field(default_factory=list)


# ─── sniffer state machine ─────────────────────────────────────────────────

# I²C protocol primer for the state machine below:
#   IDLE        : both SDA and SCL high
#   START       : SDA falls while SCL is high
#   STOP        : SDA rises while SCL is high
#   bit transfer: SDA must be stable while SCL is high; SDA changes happen
#                 while SCL is low.  Each byte = 8 SCL pulses + 1 ACK pulse.

class _SnifferState:
    """Holds the running bit-decoder state.  Edge callbacks mutate this."""

    def __init__(self, output: queue.Queue[Transaction]) -> None:
        self.out = output
        # Most-recent levels of each line (we update these on every edge).
        self.sda_level = 1
        self.scl_level = 1
        # The transaction currently being assembled.
        self.in_txn = False        # True between START and STOP
        self.bit_pos = 0           # 0..7 across the current byte
        self.byte_acc = 0          # bits accumulated for the current byte
        self.bit_count = 0         # 0..8 (8 = ACK bit slot)
        self.first_byte = True     # next completed byte is the address
        self.cur_txn: Optional[Transaction] = None
        # Diagnostics
        self.glitches = 0


def _on_sda_edge(state: _SnifferState, new_level: int, scl_level: int) -> None:
    """SDA changed.  If SCL is high while this happens, it's a START or STOP."""
    state.sda_level = new_level
    if scl_level != 1:
        return  # SDA changes during data phase — handled on SCL edges

    if new_level == 0:
        # SDA fell while SCL high → START (or repeated START)
        if state.in_txn and state.cur_txn is not None:
            # repeated start: finalize previous transaction if non-empty,
            # then begin a new one in-place
            state.out.put(state.cur_txn)
        state.cur_txn = Transaction(address=0, is_read=False, ack=False)
        state.in_txn = True
        state.bit_pos = 0
        state.byte_acc = 0
        state.bit_count = 0
        state.first_byte = True
    else:
        # SDA rose while SCL high → STOP
        if state.in_txn and state.cur_txn is not None:
            state.out.put(state.cur_txn)
        state.cur_txn = None
        state.in_txn = False
        state.bit_count = 0
        state.byte_acc = 0


def _on_scl_edge(state: _SnifferState, new_level: int, sda_level: int) -> None:
    """SCL changed.  Rising edge: sample SDA into the current byte.
    Falling edge: nothing to do (data is allowed to change after this)."""
    state.scl_level = new_level
    if new_level != 1:
        return  # only sample on rising edge
    if not state.in_txn or state.cur_txn is None:
        return  # noise outside a transaction

    if state.bit_count < 8:
        # data bit — MSB first
        state.byte_acc = (state.byte_acc << 1) | (sda_level & 1)
        state.bit_count += 1
        if state.bit_count == 8:
            # we'll see the ACK on the next rising edge
            pass
    else:
        # ACK/NAK bit slot — SDA=0 means ACK
        ack = (sda_level == 0)
        byte_val = state.byte_acc & 0xFF
        if state.first_byte:
            # address byte: top 7 bits = address, bottom = R/W
            state.cur_txn.address = (byte_val >> 1) & 0x7F
            state.cur_txn.is_read = bool(byte_val & 0x01)
            state.cur_txn.ack = ack
            state.first_byte = False
        else:
            state.cur_txn.data.append(byte_val)
        # reset for next byte
        state.byte_acc = 0
        state.bit_count = 0


# ─── public sniffer interface ──────────────────────────────────────────────

class PassiveI2cSniffer:
    """Open the GPIO chip, attach edge callbacks on SDA/SCL, and yield
    completed I²C transactions via `transactions()`.

    Filtering by I²C address is done client-side: every transaction shows
    up in the queue; callers can ignore ones that don't match.
    """

    def __init__(self, sda_pin: int = SDA_PIN, scl_pin: int = SCL_PIN, chip: int = GPIO_CHIP) -> None:
        if lgpio is None:
            raise RuntimeError(
                "lgpio not installed.  On the Pi: "
                "pip3 install --break-system-packages lgpio"
            )
        self._sda_pin = sda_pin
        self._scl_pin = scl_pin
        self._chip = chip
        self._handle: Optional[int] = None
        self._queue: queue.Queue[Transaction] = queue.Queue(maxsize=2048)
        self._state = _SnifferState(self._queue)
        # lgpio gives us a single callback-per-line; we route through a
        # tiny lock so the SDA and SCL callbacks don't trample each other.
        self._lock = threading.Lock()
        self._sda_cb_id: Optional[int] = None
        self._scl_cb_id: Optional[int] = None

    def start(self) -> None:
        if self._handle is not None:
            return
        h = lgpio.gpiochip_open(self._chip)
        lgpio.gpio_claim_input(h, self._sda_pin, lgpio.SET_PULL_NONE)
        lgpio.gpio_claim_input(h, self._scl_pin, lgpio.SET_PULL_NONE)

        # Seed initial levels so the first edge has the right context.
        self._state.sda_level = lgpio.gpio_read(h, self._sda_pin)
        self._state.scl_level = lgpio.gpio_read(h, self._scl_pin)

        # Both edges on both lines.
        lgpio.gpio_claim_alert(h, self._sda_pin, lgpio.BOTH_EDGES, lgpio.SET_PULL_NONE)
        lgpio.gpio_claim_alert(h, self._scl_pin, lgpio.BOTH_EDGES, lgpio.SET_PULL_NONE)

        def sda_cb(chip: int, gpio: int, level: int, tick: int) -> None:
            with self._lock:
                _on_sda_edge(self._state, level, self._state.scl_level)

        def scl_cb(chip: int, gpio: int, level: int, tick: int) -> None:
            with self._lock:
                _on_scl_edge(self._state, level, self._state.sda_level)

        self._sda_cb_id = lgpio.callback(h, self._sda_pin, lgpio.BOTH_EDGES, sda_cb)
        self._scl_cb_id = lgpio.callback(h, self._scl_pin, lgpio.BOTH_EDGES, scl_cb)
        self._handle = h

    def stop(self) -> None:
        h = self._handle
        if h is None:
            return
        try:
            if self._sda_cb_id is not None:
                lgpio.callback_cancel(self._sda_cb_id)
            if self._scl_cb_id is not None:
                lgpio.callback_cancel(self._scl_cb_id)
        finally:
            lgpio.gpiochip_close(h)
            self._handle = None

    def __enter__(self) -> "PassiveI2cSniffer":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # ── consumer-side API ────────────────────────────────────────────────

    def transactions(self, timeout_s: float = 1.0):
        """Generator that yields Transaction objects as they complete.
        Blocks up to `timeout_s` seconds between transactions; yields None
        on timeout so the caller can do periodic work (publish, heartbeat)."""
        while True:
            try:
                yield self._queue.get(timeout=timeout_s)
            except queue.Empty:
                yield None


# ─── CLI smoke test ────────────────────────────────────────────────────────
# Run this directly on the Pi for a few seconds to verify the wiring:
#   python3 -m predator_i2c_sniffer
# It prints every transaction it sees to stdout.  Ctrl-C to exit.

def _smoke_test() -> None:
    print(f"[sniffer] SDA=GPIO{SDA_PIN}  SCL=GPIO{SCL_PIN}  chip={GPIO_CHIP}")
    print(f"[sniffer] Watching I²C address 0x{WATCH_ADDRESS:02X}")
    n = 0
    t_start = time.monotonic()
    with PassiveI2cSniffer() as sniff:
        for txn in sniff.transactions(timeout_s=2.0):
            if txn is None:
                continue
            if txn.address != WATCH_ADDRESS:
                continue
            n += 1
            rw = "R" if txn.is_read else "W"
            ack = "ACK" if txn.ack else "NAK"
            data_str = " ".join(f"{b:02X}" for b in txn.data) or "(no data)"
            print(f"[{n:5d}] 0x{txn.address:02X} {rw} {ack}  data: {data_str}")
            if n % 100 == 0:
                rate = n / (time.monotonic() - t_start)
                print(f"[sniffer] {n} transactions, {rate:.1f}/s")


if __name__ == "__main__":
    try:
        _smoke_test()
    except KeyboardInterrupt:
        print("\n[sniffer] stopped.")
