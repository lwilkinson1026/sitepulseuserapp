"""
Predator 2000W LCD frame decoder.

The Predator power station's BMS writes 18 registers (0x00..0x11) to its
LCD controller over I²C at 7-bit address 0x3E, ~5.7 frames/second.  Each
register byte drives 8 segments of the custom segment LCD; the bit→segment
wiring differs per digit position because the segments are wired uniquely
into the LCD glass.

This module is *pure*: no I/O, no I²C, no Firestore.  It takes an 18-byte
register snapshot in and returns a dict describing what the LCD would be
displaying.  The companion `predator_i2c_sniffer.py` is responsible for
producing those snapshots from the live bus, and `firebase_publisher.py`
glues the two together and publishes the result.

Encoding notes (derived empirically — see ../docs not yet written):

  Reg 0x00, 0x01            battery percentage + battery icon + "%" symbol.
                            Custom segment encoding, NOT 7-segment digits.
                            We hold a lookup table; unknowns return None.

  Reg 0x02, 0x03, 0x04, 0x11  watts digit (1000s, 100s, 10s, 1s respectively)
                            7-segment, position-specific bit mapping.
                            Bit 7 of reg 0x11 is the "DC indicator" — set
                            only in DC mode.  Bit 7 of reg 0x02 is the
                            "thousands active" indicator.

  Reg 0x05 bit 0            "DC on" label segment (small DC indicator).
  Reg 0x07 bit 0            "output mode active" (set for both AC and DC).
  Reg 0x0B..0x0E            time-to-empty digits, HH:MM, left-to-right.
                            Bit 7 of reg 0x0C is the colon between the
                            second and third digit.
  Reg 0x0F, 0x10            label segments ("WATTS", "TIME TO EMPTY", "OUT").

Anything not in our digit tables decodes as None with a warning attached
so the publisher logs it and the scheduler stays safe (battery_soc=None
short-circuits the engine recharge loop into idle).
"""

from __future__ import annotations

from typing import Optional, TypedDict


# ─── Digit tables ──────────────────────────────────────────────────────────

# Time-to-empty digits (regs 0x0B, 0x0C, 0x0D, 0x0E).  Bit 7 of reg 0x0C is
# the colon segment between the second and third digit.
#
# Bit→segment mapping derived from observed states (g=bit3, f=bit5):
#     bit 0 = d   bit 4 = b
#     bit 1 = c   bit 5 = f
#     bit 2 = e   bit 6 = a
#     bit 3 = g   bit 7 = colon
_TIME_DIGITS = {
    0x00: " ",
    0x12: "1",
    0x3A: "4",
    0x52: "7",
    0x5B: "3",   # predicted from segment mapping; observe to confirm
    0x5D: "2",   # confirmed during live sniff (time = "52:04" / "45:51")
    0x6B: "5",
    0x6F: "6",   # predicted from segment mapping; observe to confirm
    0x77: "0",
    0x7B: "9",
    0x7F: "8",
}

# Watts digit positions (regs 0x02, 0x03, 0x04, 0x11).  Different physical
# segment wiring than the time-to-empty digits, so a different lookup.
# 0x2E ("4") and 0x5D ("2") are predicted from the bit-to-segment mapping
# I derived; they have not yet been observed live.  They'll get confirmed
# the first time a load lands in the right range.
_WATTS_DIGITS = {
    0x00: " ",
    0x24: "1",
    0x25: "7",
    0x2E: "4",   # predicted from segment mapping
    0x5D: "2",   # predicted from segment mapping
    0x6B: "5",
    0x6D: "3",
    0x6F: "9",
    0x77: "0",
    0x7B: "6",
    0x7F: "8",
}

# Battery percentage uses a custom segment layout we have not yet fully
# reverse engineered.  Add observed (reg0x00, reg0x01) pairs here as you
# capture them.  Unknown pairs decode to None so the scheduler fails safe.
_BATTERY_SOC_LOOKUP: dict[tuple[int, int], int] = {
    (0xFE, 0xD7): 85,
    (0xFE, 0x5D): 78,
    (0x4A, 0xF7): 76,   # photographed during live session, IMG_5574
    (0x4A, 0xD7): 73,   # confirmed during heater test in same session
    (0x4A, 0xDB): 72,   # photographed live, IMG_5578
    # As you observe more known percentages, append rows like:
    #   (0xFE, 0x42): 70,
    # The publisher logs every unknown pair so you can backfill this table.
}


# ─── Public types ──────────────────────────────────────────────────────────

class DecodedFrame(TypedDict):
    """Result of decoding one 18-byte register snapshot."""
    battery_soc:           Optional[int]   # 0-100, None if SoC bytes are unmapped
    dc_active:             bool            # DC outlet button currently enabled (bit 7 of reg 0x11)
    ac_active:             bool            # AC outlet button currently enabled (deduced from bus pattern)
    output_mode:           str             # "AC", "DC", "AC+DC", "off" (derived for app convenience)
    output_watts:          Optional[int]   # current draw, None if any digit unmapped
    time_to_empty_minutes: Optional[int]   # parsed HH:MM, None if unmapped or colon missing
    system_mode:           str             # "discharging", "idle" (matches scheduler.py)
    warnings:              list[str]       # human-readable list of unmapped bytes


# ─── Helpers ───────────────────────────────────────────────────────────────

def _watts_digit(byte: int, warnings: list[str], position: str) -> Optional[str]:
    """Strip the indicator bit (bit 7) and look up the digit.  Returns the
    digit character ('0'-'9') or None if unknown.  Appends a warning string
    if unknown so the publisher can log it."""
    base = byte & 0x7F
    digit = _WATTS_DIGITS.get(base)
    if digit is None:
        warnings.append(f"unknown watts digit at {position}: byte=0x{byte:02X} stripped=0x{base:02X}")
        return None
    return digit


def _time_digit(byte: int, allow_colon: bool, warnings: list[str], position: str) -> tuple[Optional[str], bool]:
    """Return (digit_char, colon_visible).  When allow_colon is True, bit 7
    is treated as the colon segment; otherwise it's just looked up as-is."""
    colon = bool(byte & 0x80) and allow_colon
    base = byte & 0x7F if allow_colon else byte
    digit = _TIME_DIGITS.get(base)
    if digit is None:
        warnings.append(f"unknown time digit at {position}: byte=0x{byte:02X} stripped=0x{base:02X}")
        return None, colon
    return digit, colon


# ─── Public API ────────────────────────────────────────────────────────────

def decode_frame(regs: list[int]) -> DecodedFrame:
    """Decode an 18-byte register snapshot (regs[0x00]..regs[0x11]) into a
    structured display state.  Never raises — unknown bytes show up as
    None fields and entries in warnings[].

    Args:
        regs: list of 18 integers, indexed by register address (0..17).

    Returns:
        DecodedFrame dict ready to merge into the Firestore snapshot.
    """
    if len(regs) != 18:
        raise ValueError(f"decode_frame expects 18 bytes, got {len(regs)}")

    warnings: list[str] = []

    # ── battery percentage ────────────────────────────────────────────────
    soc_key = (regs[0x00], regs[0x01])
    battery_soc = _BATTERY_SOC_LOOKUP.get(soc_key)
    if battery_soc is None:
        warnings.append(
            f"unknown SoC bytes: reg00=0x{regs[0x00]:02X} reg01=0x{regs[0x01]:02X} "
            f"— add to _BATTERY_SOC_LOOKUP"
        )

    # ── output mode (DC / AC / both / off) ────────────────────────────────
    # bit 7 of reg 0x11 is the DC outlet button indicator (set when DC is
    # toggled on, independent of whether the AC outlet is also enabled).
    # AC active is detected by ANY watts being drawn that isn't DC's draw —
    # but since we don't easily distinguish, we currently use reg 0x07 bit 0
    # as "some output active" and infer AC by elimination.  This is imperfect
    # when both are on; future work: identify the AC-specific bit.
    output_active = bool(regs[0x07] & 0x01)
    dc_active     = bool(regs[0x11] & 0x80)
    # If output is active but DC isn't, AC must be.  When both could be on,
    # this still flags AC correctly as long as DC isn't the only output.
    ac_active     = output_active and not dc_active
    # When both are on we currently can't see it from one byte; the publisher
    # log + raw_frame_hex helps refine this as more states are observed.

    if not output_active:
        output_mode = "off"
    elif dc_active and ac_active:
        output_mode = "AC+DC"
    elif dc_active:
        output_mode = "DC"
    else:
        output_mode = "AC"

    # ── watts (1-4 digits across reg 0x02, 0x03, 0x04, 0x11) ──────────────
    if output_mode == "off":
        output_watts: Optional[int] = None
    else:
        d_thousands = _watts_digit(regs[0x02], warnings, "1000s")
        d_hundreds  = _watts_digit(regs[0x03], warnings, "100s")
        d_tens      = _watts_digit(regs[0x04], warnings, "10s")
        d_ones      = _watts_digit(regs[0x11], warnings, "1s")

        if None in (d_thousands, d_hundreds, d_tens, d_ones):
            output_watts = None
        else:
            digits_str = (d_thousands + d_hundreds + d_tens + d_ones).lstrip(" ")
            output_watts = int(digits_str) if digits_str else 0

    # ── time-to-empty (HH:MM across reg 0x0B, 0x0C+colon, 0x0D, 0x0E) ─────
    if output_mode == "off":
        time_to_empty_minutes: Optional[int] = None
    else:
        d1, _     = _time_digit(regs[0x0B], False, warnings, "time-1")
        d2, colon = _time_digit(regs[0x0C], True,  warnings, "time-2")
        d3, _     = _time_digit(regs[0x0D], False, warnings, "time-3")
        d4, _     = _time_digit(regs[0x0E], False, warnings, "time-4")

        if None in (d1, d2, d3, d4) or not colon:
            time_to_empty_minutes = None
            if not colon and not warnings:
                warnings.append(f"time colon (reg 0x0C bit 7) not set; got 0x{regs[0x0C]:02X}")
        else:
            try:
                hours   = int(d1 + d2)
                minutes = int(d3 + d4)
                time_to_empty_minutes = hours * 60 + minutes
                # The BMS shows "99:59" as a placeholder when load is too
                # small to compute remaining time.  Surface that as None
                # instead of an absurd 5999-minute value.
                if hours == 99 and minutes == 59:
                    time_to_empty_minutes = None
            except ValueError:
                time_to_empty_minutes = None

    # ── system_mode (matches the field scheduler.py and the app expect) ──
    # We can't yet detect charging from the LCD bytes (no charging-input
    # captured yet).  Discharging = any output active.  Otherwise idle.
    if output_mode in ("AC", "DC"):
        system_mode = "discharging"
    else:
        system_mode = "idle"

    return DecodedFrame(
        battery_soc=battery_soc,
        dc_active=dc_active,
        ac_active=ac_active,
        output_mode=output_mode,
        output_watts=output_watts,
        time_to_empty_minutes=time_to_empty_minutes,
        system_mode=system_mode,
        warnings=warnings,
    )


# ─── Frame assembly from raw I²C transactions ─────────────────────────────

class FrameAssembler:
    """Accumulates per-register writes into 18-byte snapshots.

    The sniffer feeds in one transaction at a time via `feed()`.  A complete
    frame is emitted when the register number rolls back to a lower value
    (the BMS writes 0x00..0x11 in order, then loops).  The assembler stays
    forgiving: a stray write to an unexpected register is accepted, and we
    only emit a snapshot once we've seen all 18 registers since the last
    rollover.
    """

    def __init__(self) -> None:
        self._cur: dict[int, int] = {}
        self._prev_reg: Optional[int] = None

    def feed(self, reg: int, val: int) -> Optional[list[int]]:
        """Feed one (register, value) pair.  Returns the completed 18-byte
        snapshot (list indexed 0..17) when a frame boundary is crossed and
        the previous frame had all 18 registers; otherwise None."""
        snapshot: Optional[list[int]] = None
        if self._prev_reg is not None and reg < self._prev_reg:
            # Frame boundary: register number wrapped back to a lower value.
            if len(self._cur) >= 18 and set(self._cur.keys()) >= set(range(0x12)):
                snapshot = [self._cur[i] for i in range(0x12)]
            self._cur = {}

        self._cur[reg] = val
        self._prev_reg = reg
        return snapshot
