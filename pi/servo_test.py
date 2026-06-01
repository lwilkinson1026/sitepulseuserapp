"""
Bench sanity-check for one servo on the V1246 (PCA9685) board.

Run on the Pi:
    python3 -u pi/servo_test.py            # defaults to channel 0
    python3 -u pi/servo_test.py 1          # choke (production)
    python3 -u pi/servo_test.py 2          # LCD wake button servo

Wiring assumed:
    PCA9685 SDA -> Pi BCM 2 (SDA1)
    PCA9685 SCL -> Pi BCM 3 (SCL1)
    PCA9685 VCC -> Pi 3.3V (logic only)
    PCA9685 GND -> Pi GND
    PCA9685 V+  -> external 5V supply (servo power; NOT the Pi 5V pin)
    Servo signal/+/-  -> PCA9685 channel header (per arg)

NOTE: stop the listener first (`sudo systemctl stop sitepulse-listener`)
or you'll be fighting it for I2C bus access.

REPL commands:
    <number>   set angle (0-180 degrees)
    s          slow sweep 0 -> 180 -> 0
    c          center (90)
    d          detach (PWM off; servo goes limp)
    q          quit (detaches on exit)

No Firestore, no command listener — this is the dumbest test that proves
the chip, the bus, the library, and the servo all work together.
"""

from __future__ import annotations

import sys
import time

import board
import busio
from adafruit_pca9685 import PCA9685
from adafruit_motor import servo as servo_mod

CHANNEL = int(sys.argv[1]) if len(sys.argv) > 1 else 0
if not 0 <= CHANNEL <= 15:
    print(f"channel out of range: {CHANNEL} (PCA9685 has 0-15)", file=sys.stderr)
    sys.exit(2)
PWM_FREQ_HZ = 50          # standard hobby servo
MIN_PULSE_US = 1000       # conservative; tune per servo later
MAX_PULSE_US = 2000


def make_servo() -> tuple[PCA9685, servo_mod.Servo]:
    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c)
    pca.frequency = PWM_FREQ_HZ
    s = servo_mod.Servo(
        pca.channels[CHANNEL],
        min_pulse=MIN_PULSE_US,
        max_pulse=MAX_PULSE_US,
    )
    return pca, s


def detach(pca: PCA9685) -> None:
    """Cut the PWM signal so the servo relaxes (no holding torque)."""
    pca.channels[CHANNEL].duty_cycle = 0


def sweep(s: servo_mod.Servo, step_deg: float = 2.0, dwell_s: float = 0.02) -> None:
    print(f"[sweep] 0 -> 180 -> 0 (step {step_deg}deg, dwell {dwell_s}s)")
    angle = 0.0
    while angle <= 180.0:
        s.angle = angle
        time.sleep(dwell_s)
        angle += step_deg
    while angle >= 0.0:
        s.angle = max(0.0, angle)
        time.sleep(dwell_s)
        angle -= step_deg


def main() -> int:
    print(f"[init] PCA9685 @ 0x40, channel {CHANNEL}, {PWM_FREQ_HZ} Hz, "
          f"pulse {MIN_PULSE_US}-{MAX_PULSE_US} us")
    pca, s = make_servo()

    print("[init] commanding center (90 deg)")
    s.angle = 90
    time.sleep(0.5)

    try:
        while True:
            try:
                line = input("servo> ").strip().lower()
            except EOFError:
                break
            if not line:
                continue
            if line in ("q", "quit", "exit"):
                break
            if line in ("s", "sweep"):
                sweep(s)
                continue
            if line in ("c", "center"):
                s.angle = 90
                continue
            if line in ("d", "detach"):
                detach(pca)
                print("[detach] PWM off")
                continue
            try:
                angle = float(line)
            except ValueError:
                print(f"  unknown: {line!r}  (try a number 0-180, s, c, d, q)")
                continue
            if not 0.0 <= angle <= 180.0:
                print(f"  out of range: {angle} (allowed 0-180)")
                continue
            s.angle = angle
    finally:
        print("[exit] detaching servo")
        try:
            detach(pca)
            pca.deinit()
        except Exception as e:
            print(f"[exit] deinit error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
