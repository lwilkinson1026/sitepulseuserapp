#!/usr/bin/env python3
"""Breadboard simulator for the electronic button presser.

Replaces the servo "finger" that pushes the Predator's LCD-wake / AC-toggle
buttons. In the final build a Pi GPIO drives the LED side of an optocoupler
whose output transistor is soldered across one of the button's PCB pads;
lighting the LED closes the transistor, which is electrically identical to a
finger pressing the button. On the bench we stand in for that with:

  OUT  (default GPIO 24, header pin 18)  "press" line -> 330 ohm -> LED -> GND
       LED lit == button pressed. Exactly what the opto's input side sees.
  IN   (default GPIO 5, header pin 29)   momentary pushbutton -> GND
       Internal pull-up; pressed == reads 0. Lets us close the loop and
       practice reading a press back.

Raw lgpio on gpiochip0 (Pi 5, kernel >= 6.6). No RPi.GPIO, no pigpio, and
no lgpio.callback() -- the input is polled, per the lesson in
predator_i2c_sniffer.py.

Usage (run on the Pi):
  bench_button_sim.py on|off [--seconds N]        hold OUT high/low
  bench_button_sim.py blink  [--count 5 --on 0.5 --off 0.5]
  bench_button_sim.py press  [--hold 0.4 --count 1 --gap 1.0]
                                                   0.4 = LCD-wake, 0.5 = AC-toggle
  bench_button_sim.py watch  [--seconds 30]        print IN press/release events
  bench_button_sim.py mirror [--seconds 30]        LED lit while IN is held
  bench_button_sim.py toggle [--seconds 30]        each IN press flips the LED
                                                   (Predator AC-button behaviour;
                                                   --seconds 0 = run until Ctrl-C)
  bench_button_sim.py selftest                     OUT high/low with readback

Common flags: --channel lcd|ac (GPIO 24 / GPIO 16 on the button-press board),
              --out N, --in N, --active-low (invert OUT sense),
              --open-drain (pressed = drive low, released = float hi-Z; this
              is the no-optocoupler wiring where the GPIO sits directly on
              the button's pull-up pad through ~1k. Bench mimic: 3V3 ->
              330R -> LED -> GPIO, LED lights when the pin sinks).
Ctrl-C always releases OUT and frees the lines.
"""
from __future__ import annotations

import argparse
import sys
import time

import lgpio

CHIP = 0
PRESS_MIN_S, PRESS_MAX_S = 0.05, 2.0   # same clamp as buttons.wake_lcd / press_ac
POLL_S = 0.002                          # 500 Hz input poll
DEBOUNCE_S = 0.02

_h = None
_out = None
_inp = None
_active_low = False
_open_drain = False


def _ts() -> str:
    return time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"


def _open(out_pin: int | None, in_pin: int | None, active_low: bool,
          open_drain: bool = False) -> None:
    global _h, _out, _inp, _active_low, _open_drain
    _active_low = active_low
    _open_drain = open_drain
    _h = lgpio.gpiochip_open(CHIP)
    if out_pin is not None:
        if open_drain:
            rc = lgpio.gpio_claim_input(_h, out_pin, lgpio.SET_PULL_NONE)  # released = hi-Z
        else:
            rc = lgpio.gpio_claim_output(_h, out_pin, _level(False))
        if rc < 0:
            sys.exit(f"claim OUT GPIO{out_pin} failed rc={rc} "
                     f"(already owned? check `gpioinfo | grep GPIO{out_pin}`)")
        _out = out_pin
    if in_pin is not None:
        rc = lgpio.gpio_claim_input(_h, in_pin, lgpio.SET_PULL_UP)
        if rc < 0:
            sys.exit(f"claim IN GPIO{in_pin} failed rc={rc}")
        _inp = in_pin


def _close() -> None:
    global _h
    if _h is None:
        return
    try:
        if _out is not None:
            set_pressed(False)                          # never exit "pressed"
            lgpio.gpio_free(_h, _out)                   # (open-drain: pad is left as a floating input)
        if _inp is not None:
            lgpio.gpio_free(_h, _inp)
    finally:
        lgpio.gpiochip_close(_h)
        _h = None


def _level(pressed: bool) -> int:
    return (0 if pressed else 1) if _active_low else (1 if pressed else 0)


def set_pressed(pressed: bool) -> None:
    if _open_drain:
        # Never drive high: the pad may sit at 5 V. Pressed = sink to GND,
        # released = let go of the line entirely (input, no pull).
        lgpio.gpio_free(_h, _out)
        if pressed:
            lgpio.gpio_claim_output(_h, _out, 0)
        else:
            lgpio.gpio_claim_input(_h, _out, lgpio.SET_PULL_NONE)
        return
    lgpio.gpio_write(_h, _out, _level(pressed))


def out_is_pressed() -> bool:
    if _open_drain:
        # Pressed: we hold it low. Released: the external pull-up should
        # lift it, so reading 0 while released means something is wrong.
        return lgpio.gpio_read(_h, _out) == 0
    return lgpio.gpio_read(_h, _out) == _level(True)


def in_is_pressed() -> bool:
    return lgpio.gpio_read(_h, _inp) == 0   # pull-up: pressed shorts to GND


# ---------------------------------------------------------------- commands ---

def cmd_static(pressed: bool, seconds: float) -> None:
    global _out
    set_pressed(pressed)
    word = "PRESSED (LED on)" if pressed else "released (LED off)"
    print(f"{_ts()} OUT GPIO{_out} -> {word}; readback={'pressed' if out_is_pressed() else 'released'}")
    if seconds > 0:
        print(f"holding {seconds:.1f}s, Ctrl-C to release early")
        time.sleep(seconds)
    else:
        # Leave the pad as-is on exit. Verify with: pinctrl get <pin>
        lgpio.gpio_free(_h, _out)
        _out = None


def cmd_blink(count: int, on_s: float, off_s: float) -> None:
    for i in range(count):
        set_pressed(True);  print(f"{_ts()} blink {i+1}/{count} ON");  time.sleep(on_s)
        set_pressed(False); print(f"{_ts()} blink {i+1}/{count} off"); time.sleep(off_s)


def cmd_press(hold_s: float, count: int, gap_s: float) -> None:
    hold_s = max(PRESS_MIN_S, min(PRESS_MAX_S, hold_s))
    for i in range(count):
        t0 = time.monotonic()
        set_pressed(True)
        print(f"{_ts()} press {i+1}/{count}: DOWN (readback={'ok' if out_is_pressed() else 'MISMATCH'})")
        time.sleep(hold_s)
        set_pressed(False)
        dt = time.monotonic() - t0
        if out_is_pressed():
            rb = ("pad reads low while released: no external pull-up on this pin"
                  if _open_drain else "MISMATCH")
        else:
            rb = "ok"
        print(f"{_ts()} press {i+1}/{count}: UP   held {dt*1000:.0f} ms (readback={rb})")
        if i < count - 1:
            time.sleep(gap_s)


def _poll_input(seconds: float, on_press, on_release) -> None:
    """Debounced edge detection by polling. Calls on_press()/on_release(hold_s)."""
    deadline = time.monotonic() + seconds if seconds > 0 else float("inf")
    state = in_is_pressed()
    last_change = time.monotonic()
    down_at = None
    span = f"for {seconds:.0f}s" if seconds > 0 else "until Ctrl-C"
    print(f"{_ts()} IN GPIO{_inp} starts {'PRESSED' if state else 'released'}; "
          f"watching {span}")
    while time.monotonic() < deadline:
        now = time.monotonic()
        cur = in_is_pressed()
        if cur != state and (now - last_change) >= DEBOUNCE_S:
            state = cur
            last_change = now
            if state:
                down_at = now
                on_press()
            else:
                on_release((now - down_at) if down_at else 0.0)
        time.sleep(POLL_S)


def cmd_watch(seconds: float) -> None:
    n = [0]
    def dn():
        n[0] += 1; print(f"{_ts()} #{n[0]} PRESS")
    def up(held):
        print(f"{_ts()} #{n[0]} release  held {held*1000:.0f} ms")
    _poll_input(seconds, dn, up)
    print(f"total presses: {n[0]}")


def cmd_mirror(seconds: float) -> None:
    def dn():
        set_pressed(True);  print(f"{_ts()} button DOWN -> LED on")
    def up(held):
        set_pressed(False); print(f"{_ts()} button UP   -> LED off  (held {held*1000:.0f} ms)")
    _poll_input(seconds, dn, up)


def cmd_toggle(seconds: float) -> None:
    """AC-toggle semantics: a press flips state, and there is no readback in
    production -- so we track it here to see how easy it is to lose sync."""
    led = [False]
    print("each press flips the LED; LED starts off")
    def dn():
        led[0] = not led[0]
        set_pressed(led[0])
        print(f"{_ts()} press -> LED {'ON' if led[0] else 'off'}")
    def up(held):
        pass
    _poll_input(seconds, dn, up)


def cmd_selftest() -> None:
    ok = True
    for pressed in (True, False, True, False):
        set_pressed(pressed)
        time.sleep(0.05)
        got = out_is_pressed()
        flag = "ok" if got == pressed else "FAIL"
        ok &= got == pressed
        print(f"OUT GPIO{_out} write={'pressed' if pressed else 'released'} "
              f"read={'pressed' if got else 'released'} {flag}")
    print(f"IN  GPIO{_inp} idle level = {lgpio.gpio_read(_h, _inp)} "
          f"(expect 1 with pull-up and button not pressed)")
    print("SELFTEST", "PASS" if ok else "FAIL")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--channel", choices=("lcd", "ac"),
                   help="board channel: lcd = GPIO 24 (YEL), ac = GPIO 16 (GRN); sets --out")
    p.add_argument("--out", type=int, default=None, help="OUT GPIO (BCM), default 24 = header pin 18")
    p.add_argument("--in", dest="inp", type=int, default=5, help="IN GPIO (BCM), default 5 = header pin 29")
    p.add_argument("--active-low", action="store_true", help="OUT low == pressed")
    p.add_argument("--open-drain", action="store_true",
                   help="pressed = sink low, released = float (direct-to-pad wiring, no opto)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("on");    s.add_argument("--seconds", type=float, default=0)
    s = sub.add_parser("off");   s.add_argument("--seconds", type=float, default=0)
    s = sub.add_parser("blink"); s.add_argument("--count", type=int, default=5)
    s.add_argument("--on", type=float, default=0.5); s.add_argument("--off", type=float, default=0.5)
    s = sub.add_parser("press"); s.add_argument("--hold", type=float, default=0.4)
    s.add_argument("--count", type=int, default=1); s.add_argument("--gap", type=float, default=1.0)
    for name in ("watch", "mirror", "toggle"):
        s = sub.add_parser(name); s.add_argument("--seconds", type=float, default=30)
    sub.add_parser("selftest")

    a = p.parse_args()
    if a.out is None:
        a.out = {"lcd": 24, "ac": 16, None: 24}[a.channel]
    needs_in = a.cmd in ("watch", "mirror", "toggle", "selftest")
    needs_out = a.cmd != "watch"
    if a.open_drain and a.active_low:
        sys.exit("--open-drain already means low == pressed; drop --active-low")
    _open(a.out if needs_out else None, a.inp if needs_in else None, a.active_low, a.open_drain)
    try:
        if a.cmd == "on":       cmd_static(True, a.seconds)
        elif a.cmd == "off":    cmd_static(False, a.seconds)
        elif a.cmd == "blink":  cmd_blink(a.count, a.on, a.off)
        elif a.cmd == "press":  cmd_press(a.hold, a.count, a.gap)
        elif a.cmd == "watch":  cmd_watch(a.seconds)
        elif a.cmd == "mirror": cmd_mirror(a.seconds)
        elif a.cmd == "toggle": cmd_toggle(a.seconds)
        elif a.cmd == "selftest": cmd_selftest()
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        _close()


if __name__ == "__main__":
    main()
