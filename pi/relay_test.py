#!/usr/bin/env python3
"""
Bench tool: click each relay on the Waveshare board on and off by hand.

    ssh -t lwilkinson@192.168.1.101 'python3 ~/sitepulse/relay_test.py'

Talks in PHYSICAL terms — "energized" means the coil is on (NO closed, NC
open), "released" means coil off — because that is what you hear click and
what decides which contact a load is on. The listener's logical/inverted
channel modes (RELAY_INVERT) are deliberately ignored here.

Drives the pins with `pinctrl`, which writes the GPIO registers directly, so
it works while sitepulse-listener still holds the pins. The listener will not
notice; its override watcher re-drives ch1 only when the toggle switch moves.
On quit every pin is put back exactly where it was found.

Pin map defaults to UNIT-001 (26/20/21). Override: SITEPULSE_RELAY_<n>_PIN.
"""

import os
import subprocess
import sys

PINS = {
    1: int(os.environ.get("SITEPULSE_RELAY_1_PIN", "26")),
    2: int(os.environ.get("SITEPULSE_RELAY_2_PIN", "20")),
    3: int(os.environ.get("SITEPULSE_RELAY_3_PIN", "21")),
}
LABELS = {1: "ch1 (light / enclosure fan)", 2: "ch2 (spark)", 3: "ch3 (engine fan, NC)"}
ACTIVE_LOW = os.environ.get("SITEPULSE_RELAY_ACTIVE_LOW", "1") == "1"


def read_level(pin: int) -> str:
    out = subprocess.run(["pinctrl", "get", str(pin)], capture_output=True, text=True).stdout
    return "lo" if "| lo" in out else "hi"


def energized(pin: int) -> bool:
    return (read_level(pin) == "lo") == ACTIVE_LOW


def set_energized(pin: int, on: bool) -> None:
    level = "dl" if on == ACTIVE_LOW else "dh"
    subprocess.run(["pinctrl", "set", str(pin), "op", level], check=True)


def show() -> None:
    for ch, pin in PINS.items():
        state = "ENERGIZED (NO closed)" if energized(pin) else "released  (NC closed)"
        print(f"  [{ch}] GPIO{pin:<3} {state}   {LABELS[ch]}")


def main() -> None:
    original = {pin: read_level(pin) for pin in PINS.values()}
    print("Relay bench test. Keys: 1/2/3 toggle a channel, a = all on, z = all off, q = quit (restores).")
    try:
        while True:
            show()
            try:
                key = input("> ").strip().lower()
            except EOFError:
                break
            if key == "q":
                break
            elif key in ("1", "2", "3"):
                pin = PINS[int(key)]
                set_energized(pin, not energized(pin))
            elif key == "a":
                for pin in PINS.values():
                    set_energized(pin, True)
            elif key == "z":
                for pin in PINS.values():
                    set_energized(pin, False)
            else:
                print("  ? use 1, 2, 3, a, z or q")
    finally:
        for pin, level in original.items():
            subprocess.run(["pinctrl", "set", str(pin), "op", "dl" if level == "lo" else "dh"])
        print("restored:")
        show()


if __name__ == "__main__":
    if subprocess.run(["which", "pinctrl"], capture_output=True).returncode != 0:
        sys.exit("pinctrl not found — this must run on the Pi")
    main()
