"""Regression tests for predator_decoder, built from real UNIT-002 frames.

Run:  python3 pi/test_predator_decoder.py      (or: python3 -m pytest pi/)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from predator_decoder import decode_frame, decode_frames_majority  # noqa: E402


def H(hex_str: str) -> list[int]:
    return [int(b, 16) for b in hex_str.split()]


# Captured 2026-09-25 while the engine regen-charged UNIT-002 through the 48 V
# battery connection (VESC amps_in -8..-13 A). Landon, at the unit, confirmed
# the LCD kept showing the normal screen with a ~40 W DC load. Reg 0x0F/0x10
# read 00/00 here even so.
ENGINE_CHARGING = [
    H("5C DB 80 00 2E 87 F8 01 00 F7 F6 F7 DD 92 BA 00 00 DD"),  # 42 W
    H("5C DB 80 00 2E FF F8 01 00 FF DE F7 DD 92 DB 00 00 F7"),  # 40 W
    H("5C DB 80 00 6D FF F8 01 00 FF B6 F7 DD 92 DD 00 00 ED"),  # 33 W
    H("5C DB 80 00 6D 07 78 01 00 FF AC F7 DD 92 DD 00 00 A5"),  # 37 W
    H("5C 5D 80 00 2E 07 F8 01 00 FF 24 F7 DD 92 DD 00 00 A4"),  # 41 W
]

# Same session, seconds after the engine stopped: flags back to 18/08.
ENGINE_STOPPED = [
    H("5C D7 80 00 2E 07 F8 01 00 BB 24 77 D2 6B 5B 18 08 ED"),
    H("5C D7 80 00 6D 07 F8 01 00 BB 24 12 92 3A 5D 18 08 FF"),
]


def test_engine_charging_without_flag_keeps_old_behaviour():
    # The decoder cannot tell engine charging from a dark row on its own.
    d = decode_frame(ENGINE_CHARGING[0])
    assert d["output_watts"] is None
    assert d["output_mode"] == "off"
    assert d["ac_active"] is False


def test_engine_charging_reads_watts_and_dc():
    expected = [42, 40, 33, 37, 41]
    for frame, watts in zip(ENGINE_CHARGING, expected):
        d = decode_frame(frame, external_charging=True)
        assert d["output_watts"] == watts, (d["output_watts"], watts)
        assert d["dc_active"] is True
        assert d["ac_active"] is None          # genuinely unknown
        assert d["output_mode"] == "DC"
        assert d["system_mode"] == "discharging"
        assert d["charging"] is False           # not the wall charger
        assert d["time_to_empty_minutes"] is None
        assert d["battery_soc"] in (43, 44)


def test_engine_charging_majority_vote():
    d = decode_frames_majority(ENGINE_CHARGING, external_charging=True)
    assert d["output_watts"] in (42, 40, 33, 37, 41)
    assert d["dc_active"] is True
    assert d["ac_active"] is None
    assert d["output_mode"] == "DC"


def test_engine_stopped_unchanged():
    for frame in ENGINE_STOPPED:
        for flag in (False, True):   # a lit row decodes the same either way
            d = decode_frame(frame, external_charging=flag)
            assert d["output_mode"] == "DC"
            assert d["dc_active"] is True
            assert d["ac_active"] is False
            assert 38 <= d["output_watts"] <= 44


def test_asleep_panel_stays_dark_even_if_engine_charging():
    frame = ENGINE_CHARGING[0][:]
    frame[0x07] = 0x00                          # display asleep
    d = decode_frame(frame, external_charging=True)
    assert d["output_watts"] is None
    assert d["output_mode"] == "off"


def test_wall_charging_still_blanks_watts():
    # Wall charger sets reg 0x07 bits 0x24; the flag must not resurrect the
    # fictional "6 W" the output-row gate exists to suppress.
    frame = ENGINE_CHARGING[0][:]
    frame[0x07] = 0x25
    for flag in (False, True):
        d = decode_frame(frame, external_charging=flag)
        assert d["charging"] is True
        assert d["output_watts"] is None
        assert d["system_mode"] == "charging"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
