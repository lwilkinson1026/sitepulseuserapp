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
    GND     (pin 6)  — splice to the LCD's green wire (MUST be a real GND
                        pin — pin 6, 9, 14, 20, 25, 30, 34, or 39 — NOT an
                        IOnn pin, or the Pi inputs will float and pick up
                        garbage)
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
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Claim the GPIO lines and spawn the polling thread.

        We use a polling loop instead of `lgpio.callback()` for two reasons:

         1. Callbacks fire on lgpio's own background thread, which can deliver
            SDA and SCL edges in an order that doesn't match the actual hardware
            timeline — fatal for an I²C state machine where SDA must be sampled
            at the moment SCL rises.
         2. Polling lets us read both pins in the *same syscall sequence*, so
            we never get a stale view of one while looking at the other.

        At 9 kHz the bus has ~110 µs/bit; polling at ~100 kHz on a Pi 5 catches
        every edge with margin.  CPU cost is ~5% of one core — negligible.
        """
        if self._handle is not None:
            return
        h = lgpio.gpiochip_open(self._chip)

        rc_sda = lgpio.gpio_claim_input(h, self._sda_pin, lgpio.SET_PULL_NONE)
        rc_scl = lgpio.gpio_claim_input(h, self._scl_pin, lgpio.SET_PULL_NONE)
        if rc_sda < 0 or rc_scl < 0:
            lgpio.gpiochip_close(h)
            raise RuntimeError(
                f"lgpio.gpio_claim_input failed: "
                f"SDA(GPIO{self._sda_pin})={rc_sda}  SCL(GPIO{self._scl_pin})={rc_scl}  "
                "(non-zero return = error; usually 'pin already in use')"
            )

        # Seed initial levels so the first detected transition has correct
        # context (i.e., we don't mistake the boot-time idle-high state for
        # a STOP condition fired on the first poll).
        self._state.sda_level = lgpio.gpio_read(h, self._sda_pin)
        self._state.scl_level = lgpio.gpio_read(h, self._scl_pin)

        self._handle = h
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="i2c-sniff"
        )
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        """Tight loop reading SDA + SCL, dispatching to the state machine on
        every transition.  Single-threaded → no ordering races."""
        h = self._handle
        sda_pin = self._sda_pin
        scl_pin = self._scl_pin
        state = self._state
        read = lgpio.gpio_read   # local alias for speed

        while not self._stop_event.is_set():
            sda = read(h, sda_pin)
            scl = read(h, scl_pin)

            # Process SCL changes first.  The state machine for SCL rising
            # samples SDA via the fresh `sda` value we just read.
            if scl != state.scl_level:
                _on_scl_edge(state, scl, sda)
            if sda != state.sda_level:
                _on_sda_edge(state, sda, scl)

    def stop(self) -> None:
        h = self._handle
        if h is None:
            return
        self._stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None
        try:
            lgpio.gpiochip_close(h)
        finally:
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
