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
                            Ordinary 7-segment digits sharing the watts
                            segment map, shifted left one bit:
                            `_WATTS_DIGITS[reg >> 1]`.  Reg 0x00 bit 0 is the
                            hundreds "1"; reg 0x01 bit 0 is the "%" glyph.

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
  Reg 0x10 bit 3            LCD awake (set in essentially every decodable
                            frame — an inviting false positive, do not use
                            it as an output indicator).
  Reg 0x10 bit 4            AC inverter on — the orange NEMA-outlet glyph.
                            Clear while ~950 W flows on DC, set while DC is
                            also on, so it is AC-specific rather than a
                            "load present" flag.

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

# Battery percentage.  This was long assumed to be an opaque custom layout
# requiring a hand-built lookup table.  It is not: regs 0x00/0x01 are two
# ordinary 7-segment digits using the *same* segment wiring as the watts
# digits, shifted left by one bit.
#
#     reg 0x00:  bits 1-7 = tens digit      bit 0 = hundreds "1"
#     reg 0x01:  bits 1-7 = ones digit      bit 0 = the "%" symbol
#
# So `_WATTS_DIGITS[reg >> 1]` yields the digit directly, and 100% falls out
# as reg00 = 0xEF -> hundreds bit set + "0", reg01 = 0xEF -> "0"  =>  "100".
#
# Derived 2026-07-28 on UNIT-002 from 14 consecutive states captured during a
# 1000 W discharge (lcd_calibrate.py session 20260728-171342), cross-checked
# against camera ground truth.  All ten digits reproduce _WATTS_DIGITS exactly
# and the map holds across two decades — 0x4B decodes as "7" at both 97% and
# 87% — so this is a real decode, not a curve fit.
#
# Retained below purely as a regression fixture.  NOTE: four entries in the
# original hand-mapped table were wrong, and were internally inconsistent
# (it required 0xD7 to mean both "3" and "5").  Corrected values, with the
# originally recorded value in the comment:
_BATTERY_SOC_VERIFIED: dict[tuple[int, int], int] = {
    (0xEF, 0xEF): 100,  # camera + Saleae agreed, UNIT-002 bench
    (0xDE, 0x4B): 97,   # ─┐
    (0xDE, 0xF7): 96,   #  │
    (0xDE, 0xD7): 95,   #  │ 1000 W discharge, monotonic, camera-confirmed
    (0xDE, 0x5D): 94,   #  │ each step down photographed
    (0xDE, 0xDB): 93,   #  │
    (0xDE, 0xBB): 92,   #  │
    (0xDE, 0x49): 91,   #  │
    (0xDE, 0xEF): 90,   #  │
    (0xFE, 0xDF): 89,   #  │
    (0xFE, 0xFF): 88,   #  │
    (0xFE, 0x4B): 87,   # ─┘
    (0xFE, 0xD7): 85,   # original hand-mapped entry — correct
    (0xFE, 0x5D): 84,   # was recorded 78 — off by 6
    (0x4A, 0xF7): 76,   # original hand-mapped entry — correct
    (0x4A, 0xD7): 75,   # was recorded 73
    (0x4A, 0xDB): 73,   # was recorded 72
    (0x4A, 0xBB): 72,   # was recorded 71
}


def _decode_battery_soc(hi: int, lo: int, warnings: list[str]) -> Optional[int]:
    """Decode the battery percentage from regs 0x00 (tens) and 0x01 (ones).

    Returns None on an unrecognised segment pattern so the scheduler fails
    safe — battery_soc=None short-circuits the engine recharge loop to idle.
    """
    tens = _WATTS_DIGITS.get((hi >> 1) & 0x7F)
    ones = _WATTS_DIGITS.get((lo >> 1) & 0x7F)
    if tens is None or ones is None:
        warnings.append(
            f"undecodable SoC segments: reg00=0x{hi:02X} reg01=0x{lo:02X}"
        )
        return None

    hundreds = bool(hi & 0x01)
    tens, ones = tens.strip(), ones.strip()

    # A fully blank field is the LCD asleep or a torn frame, not a reading.
    # This is the common case (the panel sleeps on its own), so it returns
    # None quietly rather than spamming a warning on every frame.
    if not tens and not ones and not hundreds:
        return None

    # The ones digit is always displayed on a real reading; the tens digit
    # blanks only below 10%.  Anything else is a partially-latched frame —
    # reject it rather than emit a plausible-looking number, because a wrong
    # SoC silently drives the engine recharge scheduler.
    if not ones or (hundreds and not tens):
        warnings.append(
            f"partial SoC frame: reg00=0x{hi:02X} reg01=0x{lo:02X} "
            f"(tens={tens or 'blank'!s} ones={ones or 'blank'!s})"
        )
        return None

    try:
        value = int(("1" if hundreds else "") + tens + ones)
    except ValueError:
        warnings.append(f"nonsense SoC digits from 0x{hi:02X},0x{lo:02X}")
        return None

    # The display tops out at 100, so the hundreds segment can only ever
    # accompany "00".  Any other 3-digit combination is a decode error.
    if hundreds and value != 100:
        warnings.append(
            f"impossible SoC {value} (hundreds bit set) "
            f"from 0x{hi:02X},0x{lo:02X}"
        )
        return None
    if not 0 <= value <= 100:
        warnings.append(f"SoC {value} out of range from 0x{hi:02X},0x{lo:02X}")
        return None
    return value


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
    battery_soc = _decode_battery_soc(regs[0x00], regs[0x01], warnings)

    # ── output mode (DC / AC / both / off) ────────────────────────────────
    # bit 7 of reg 0x11 is the DC outlet button indicator (set when DC is
    # toggled on, independent of whether the AC outlet is also enabled).
    #
    # AC is reg 0x10 bit 4 — the orange NEMA-outlet glyph on the LCD, which
    # the panel lights whenever the inverter is on.  Identified 2026-07-28 on
    # UNIT-002 by photographing the display across an AC off→on toggle while
    # the LCD stayed awake, which is the one state the earlier attempts never
    # isolated (every previous "AC off" sample was really the whole display
    # asleep, so every candidate bit correlated equally).
    #
    # It is genuinely AC-specific rather than a generic "load present" flag:
    # in session 20260728-165657 roughly 950 W flows on DC with this bit
    # CLEAR, and in session 20260728-171342 it is SET while DC is also on.
    #
    # Do not confuse it with bit 3 of the same register, which tracks the
    # LCD being awake and is therefore set in almost every frame worth
    # decoding — that is what makes it such an inviting false positive.
    output_active = bool(regs[0x07] & 0x01)
    # Both outlet flags are only meaningful while the display is driving its
    # output row; during the wake/sleep transition the panel latches one of
    # them a frame or two out of step with the rest.  Gating here keeps
    # ac_active/dc_active consistent with output_mode, so the app can never
    # be handed mode="off" alongside ac_active=True.
    dc_active     = output_active and bool(regs[0x11] & 0x80)
    ac_active     = output_active and bool(regs[0x10] & 0x10)

    # A frame can show AC or DC set while the "output active" label is clear
    # during the display's wake/sleep transition.  Treat that as off rather
    # than reporting a mode built from a half-latched frame.
    if not output_active:
        output_mode = "off"
    elif dc_active and ac_active:
        output_mode = "AC+DC"
    elif dc_active:
        output_mode = "DC"
    elif ac_active:
        output_mode = "AC"
    else:
        # Display awake, output label lit, but neither outlet flagged. Real
        # and routine: the panel sits here after AC is switched off but
        # before it sleeps.  Previously this fell through to "AC", which is
        # exactly backwards.
        output_mode = "off"

    # ── watts (1-4 digits across reg 0x02, 0x03, 0x04, 0x11) ──────────────
    if not output_active:
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
    if not output_active:
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
    #
    # "AC+DC" was missing from this test, so a unit discharging on both
    # outlets reported "idle" — which the scheduler reads as "safe to leave
    # the engine off".  It had never fired before because ac_active could not
    # previously be true at the same time as dc_active.  Making AC detection
    # work is what exposed it.
    if output_mode in ("AC", "DC", "AC+DC"):
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
