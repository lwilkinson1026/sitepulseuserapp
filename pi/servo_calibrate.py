"""
Choke servo calibration REPL — drive the PCA9685 ch1 servo by raw pulse
width to find the linkage's mechanical end-stops. Bypasses Firestore and
the slew limiter entirely.

Run on the Pi (listener MUST be stopped first or it will fight us for the
chip):
    sudo systemctl stop sitepulse-listener
    cd ~/sitepulse && python3 -u servo_calibrate.py

REPL:
    <number>    set pulse width in µs (e.g. 1500)
    +           +10 µs
    -           -10 µs
    ++          +50 µs
    --          -50 µs
    ?           print current state
    d           detach (PWM off; servo goes limp)
    q           quit (detaches on exit)

Workflow:
1. Type 1500 — servo goes to mechanical center.
2. Walk one direction (+ or -) until the choke plate is at one end-stop.
   STOP before the servo buzzes/strains — that means you're past the
   linkage's range and fighting a hard stop. Write down the µs.
3. Walk the other direction the same way. Write down that µs.
4. Quit (auto-detaches).

The lower number becomes `minUs`, the higher becomes `maxUs`. Polarity
(which one corresponds to OPEN vs CLOSED) determines whether you keep
that order or swap when writing to Firestore.
"""
from __future__ import annotations

import sys

import board
import busio
from adafruit_pca9685 import PCA9685

CHANNEL = 1
PWM_FREQ_HZ = 50
MIN_PULSE_US = 800        # conservative lower bound; widen if your servo allows
MAX_PULSE_US = 2200       # conservative upper bound; widen if your servo allows
START_US = 1500


def us_to_duty(us: int) -> int:
    """Convert pulse width µs to PCA9685 16-bit duty at PWM_FREQ_HZ."""
    period_us = 1_000_000 // PWM_FREQ_HZ        # 20000 µs at 50 Hz
    return int(round(us * 65535 / period_us))


def main() -> int:
    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c)
    pca.frequency = PWM_FREQ_HZ
    print(f"[init] PCA9685 ch{CHANNEL} @ {PWM_FREQ_HZ} Hz, "
          f"allowed range {MIN_PULSE_US}-{MAX_PULSE_US} µs")
    print(f"[init] driving ch{CHANNEL} to {START_US} µs (center)")
    current = START_US
    pca.channels[CHANNEL].duty_cycle = us_to_duty(current)

    try:
        while True:
            try:
                line = input(f"[{current} µs] calibrate> ").strip().lower()
            except EOFError:
                break
            if not line:
                continue
            if line in ("q", "quit", "exit"):
                break
            if line in ("d", "detach"):
                pca.channels[CHANNEL].duty_cycle = 0
                print("  detached (PWM off)")
                continue
            if line == "?":
                print(f"  current: {current} µs (duty {us_to_duty(current)})")
                continue
            if line == "++":
                target = current + 50
            elif line == "--":
                target = current - 50
            elif line == "+":
                target = current + 10
            elif line == "-":
                target = current - 10
            else:
                try:
                    target = int(line)
                except ValueError:
                    print(f"  unknown: {line!r} (try a number, +, -, ++, --, ?, d, q)")
                    continue
            if not MIN_PULSE_US <= target <= MAX_PULSE_US:
                print(f"  out of range: {target} (allowed {MIN_PULSE_US}-{MAX_PULSE_US})")
                continue
            current = target
            pca.channels[CHANNEL].duty_cycle = us_to_duty(current)
    finally:
        print("[exit] detaching")
        try:
            pca.channels[CHANNEL].duty_cycle = 0
            pca.deinit()
        except Exception as e:
            print(f"[exit] deinit error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
