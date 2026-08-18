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
  Reg 0x07 bit 0            LCD awake / panel driving its segments.  NOT
                            "output active" — it is set while charging too,
                            when nothing is being output at all.
  Reg 0x07 bits 2+5 (0x24)  Charging from the wall.  Never seen in 51 frames
                            across six discharge sessions, where reg 0x07 is
                            only ever 0x00 (asleep) or 0x01 (awake).
  Reg 0x0B..0x0E            HH:MM digits, left-to-right.  Which quantity they
                            represent depends on the layout: time-to-empty
                            while discharging, time-to-full while charging.
                            Bit 7 of reg 0x0C is the colon between the
                            second and third digit; while charging bit 7 is
                            set on *all four* digits and must be stripped.
  Reg 0x0F bits 3+4 (0x18)  The output row ("OUT" / "WATTS" labels).  Lit
                            whenever the unit is discharging, clear when it
                            is asleep *or* charging — which makes this, not
                            reg 0x07 bit 0, the real "output section" flag.
  Reg 0x10 bit 3            Part of the same output-row label group as
                            reg 0x0F.  Previously documented here as "LCD
                            awake"; that is wrong — it is clear while
                            charging, when the LCD is plainly awake.
  Reg 0x10 bit 4            AC section active.  Set when the inverter is on
                            (clear while ~950 W flows on DC, set while DC is
                            also on, so it is AC-specific rather than a
                            "load present" flag) — but ALSO set while
                            charging from the wall, where it presumably
                            marks the AC *input*.  Only meaningful when the
                            output row above is lit.

Anything not in our digit tables decodes as None with a warning attached
so the publisher logs it and the scheduler stays safe (battery_soc=None
short-circuits the engine recharge loop into idle).
"""

from __future__ import annotations

from collections import Counter
from typing import Optional, TypedDict


# ─── Flag masks ────────────────────────────────────────────────────────────

# Reg 0x07 bits 2 and 5.  Set together the moment the wall charger is plugged
# in (0x01 -> 0x25) and clear again when it comes out.  Derived 2026-07-28 on
# UNIT-002; see the register map in the module docstring for the negative
# control that makes this safe to key on.
_CHARGING_MASK = 0x24

# Reg 0x0F bits 3 and 4 — the "OUT"/"WATTS" legends, i.e. the output row.
_OUTPUT_ROW_MASK = 0x18


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
    time_to_full_minutes:  Optional[int]   # charge ETA, None unless charging
    charging:              bool            # wall charger connected and drawing
    system_mode:           str             # "charging", "discharging", "idle" (matches scheduler.py)
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


def _decode_hhmm(
    regs: list[int],
    warnings: list[str],
    label: str,
    strip_all: bool = False,
) -> Optional[int]:
    """Decode the HH:MM field at regs 0x0B..0x0E into minutes.

    The same four digits serve time-to-empty while discharging and
    time-to-full while charging, so the parsing lives here once.

    `strip_all` selects the charging layout, where bit 7 is set on all four
    digits rather than only on reg 0x0C.  Verified against the panel: regs
    F7 F7 DD FB photograph as "00:29" and F7 F7 DB 92 as "00:31", both of
    which only decode once bit 7 is stripped from every digit.
    """
    # The colon lives on reg 0x0C in both layouts.  In the charging layout the
    # other three bit-7s are the layout marker rather than colons, so they are
    # stripped for their digit value but discarded as colon evidence.
    d1, _     = _time_digit(regs[0x0B], strip_all, warnings, f"{label}-1")
    d2, colon = _time_digit(regs[0x0C], True,      warnings, f"{label}-2")
    d3, _     = _time_digit(regs[0x0D], strip_all, warnings, f"{label}-3")
    d4, _     = _time_digit(regs[0x0E], strip_all, warnings, f"{label}-4")
    if None in (d1, d2, d3, d4):
        return None
    if not colon:
        warnings.append(
            f"{label} colon (reg 0x0C bit 7) not set; got 0x{regs[0x0C]:02X}"
        )
        return None
    try:
        hours   = int(d1 + d2)
        minutes = int(d3 + d4)
    except ValueError:
        return None
    # The BMS shows "99:59" as a placeholder when it cannot compute a
    # meaningful estimate (load too small, or charge just started).  Surface
    # that as None instead of an absurd 5999-minute value.
    if hours == 99 and minutes == 59:
        return None
    return hours * 60 + minutes


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
    # Do not confuse it with bit 3 of the same register.  That bit was once
    # documented here as "LCD awake", which is wrong: it stays CLEAR through
    # an entire wall charge while the panel is plainly lit.  Its real meaning
    # is still unknown, so nothing keys on it.
    #
    # "LCD awake" is reg 0x07 bit 0, below.  It only means the panel is
    # driving its segments — it is set while charging too, so it is not an
    # output flag (see _OUTPUT_ROW_MASK for the one that is).
    display_awake = bool(regs[0x07] & 0x01)

    # Charging from the wall.  Reg 0x07 has never been anything but 0x00
    # (asleep) or 0x01 (awake) in 51 saved frames across six discharge
    # sessions; plugging the charger in takes it to 0x25.  Both new bits are
    # required rather than either one, because they have only ever been
    # observed moving together and demanding both fails safe.
    charging = display_awake and (regs[0x07] & _CHARGING_MASK) == _CHARGING_MASK

    # The output row.  This is the flag that actually distinguishes "putting
    # power out" from "taking power in": reg 0x0F is 0x18 in every single
    # discharge frame we have, and 0x00 both when the panel sleeps and while
    # it charges.  Photographed directly — during a charge the "OUT" and
    # "WATTS" legends are unlit silkscreen while the HH:MM field is still
    # emissive.
    output_section = display_awake and not charging and bool(regs[0x0F] & _OUTPUT_ROW_MASK)

    # Both outlet flags are only meaningful while the output row is lit.
    # During the wake/sleep transition the panel latches one of them a frame
    # or two out of step with the rest, and while charging reg 0x10 bit 4 is
    # set to mark the AC *input*.  Gating on output_section keeps
    # ac_active/dc_active consistent with output_mode, so the app can never
    # be handed mode="off" or system_mode="charging" alongside ac_active=True.
    dc_active     = output_section and bool(regs[0x11] & 0x80)
    ac_active     = output_section and bool(regs[0x10] & 0x10)

    if charging and (regs[0x0F] or not any(regs[0x08:0x0B])):
        # Three independent things move when the charger goes in: reg 0x07
        # gains 0x24, reg 0x0F drops to 0x00, and regs 0x08-0x0A come alive
        # (they are 0x00 in every discharge frame ever captured).  If they
        # disagree we are looking at a layout this decoder has not seen, so
        # say so rather than quietly trusting one bit pair.
        warnings.append(
            f"charging flag set but layout disagrees: "
            f"0x0F=0x{regs[0x0F]:02X} 0x08-0x0A="
            f"{regs[0x08]:02X},{regs[0x09]:02X},{regs[0x0A]:02X}"
        )

    # A frame can show AC or DC set while the output row is clear during the
    # display's wake/sleep transition.  Treat that as off rather than
    # reporting a mode built from a half-latched frame.
    if not output_section:
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
    # Gated on the output row, not merely on the display being awake.  While
    # charging the WATTS legend is dark and these registers hold 00/80/80/xx,
    # which used to decode to a confident-looking "6 W" of output that was
    # entirely fictional.
    if not output_section:
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

    # ── HH:MM (regs 0x0B, 0x0C+colon, 0x0D, 0x0E) ─────────────────────────
    # One field on the glass, two meanings.  The panel relabels it
    # "TIME TO FULL" while charging, so route it to whichever output field
    # matches the current layout and leave the other None — reporting a
    # charge ETA as "time to empty" would be worse than reporting nothing.
    time_to_empty_minutes: Optional[int] = None
    time_to_full_minutes: Optional[int] = None
    if charging:
        time_to_full_minutes = _decode_hhmm(regs, warnings, "time-to-full", strip_all=True)
    elif output_section:
        time_to_empty_minutes = _decode_hhmm(regs, warnings, "time-to-empty")

    # ── system_mode (matches the field scheduler.py and the app expect) ──
    # Charging wins over everything: a unit on the wall charger is not idle,
    # and it is emphatically not discharging.  Before this existed the
    # charging layout decoded as system_mode="discharging" on output_mode="AC"
    # with a fictional 6 W load, which is precisely the reading that would
    # tell the supervisor to fire the engine to recharge a battery that is
    # already charging from mains.
    #
    # "AC+DC" was missing from this test, so a unit discharging on both
    # outlets reported "idle" — which the scheduler reads as "safe to leave
    # the engine off".  It had never fired before because ac_active could not
    # previously be true at the same time as dc_active.  Making AC detection
    # work is what exposed it.
    if charging:
        system_mode = "charging"
    elif output_mode in ("AC", "DC", "AC+DC"):
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
        time_to_full_minutes=time_to_full_minutes,
        charging=charging,
        system_mode=system_mode,
        warnings=warnings,
    )


# ─── Frame assembly from raw I²C transactions ─────────────────────────────

# A register may be this many sweeps old and still be used to fill a gap.
# Sweeps run at ~7 Hz, so 15 caps a carried-over value at roughly two seconds
# — far tighter than the 15 s publish interval it feeds, and far tighter than
# the rate at which any of these quantities physically change.
MAX_REGISTER_AGE_SWEEPS = 15

# The display block is registers 0x00..0x11. Higher addresses appear on the
# bus occasionally (0x12, 0x16, 0x1A, 0x1C, 0x1E, 0x20 were all observed on
# UNIT-002) and are not part of the sweep.
_DISPLAY_REGS = range(0x12)


class FrameAssembler:
    """Accumulates per-register writes into 18-byte snapshots.

    The BMS writes registers 0x00..0x11 in ascending order and then loops, so
    a frame boundary is a register address that steps backwards.

    This originally required all 18 registers to arrive within a single sweep.
    On a clean bus that is fine; on this tap it never completes. The sniffer is
    a passive bit-banged listener and loses roughly a quarter of transactions
    (28% measured on UNIT-002, 2026-08-18), which puts the odds of eighteen
    specific registers all surviving one sweep near zero — across 171
    consecutive sweeps not one was complete, and the best was still short by
    two. The publisher reported `frames=0` with every LCD-derived field null,
    which is indistinguishable from a sleeping panel and was misread as one for
    weeks.

    So values now carry across sweeps: the assembler keeps the most recent
    value for each register and emits once it holds all eighteen, provided none
    is staler than MAX_REGISTER_AGE_SWEEPS. That is sound here specifically
    because the frame carries no checksum and each register decodes
    independently (see decode_frame) — combining a register captured one sweep
    ago with one captured now invalidates nothing. On a protocol with a frame
    checksum this would be the wrong shape entirely.
    """

    def __init__(self) -> None:
        self._latest: dict[int, int] = {}
        self._seen_at: dict[int, int] = {}
        self._prev_reg: Optional[int] = None
        self._sweep = 0

    def feed(self, reg: int, val: int) -> Optional[list[int]]:
        """Feed one (register, value) pair.  Returns an 18-byte snapshot
        (list indexed 0..17) at each frame boundary where every register is
        present and recent enough; otherwise None."""
        if reg not in _DISPLAY_REGS:
            # Ignored rather than stored: a stray high address arriving
            # mid-sweep would otherwise make the next legitimate register look
            # like a frame boundary and split one sweep into two.
            return None

        snapshot: Optional[list[int]] = None
        if self._prev_reg is not None and reg < self._prev_reg:
            snapshot = self._build()
            self._sweep += 1

        self._latest[reg] = val
        self._seen_at[reg] = self._sweep
        self._prev_reg = reg
        return snapshot

    def _build(self) -> Optional[list[int]]:
        cutoff = self._sweep - MAX_REGISTER_AGE_SWEEPS
        for reg in _DISPLAY_REGS:
            if reg not in self._latest or self._seen_at[reg] < cutoff:
                return None
        return [self._latest[reg] for reg in _DISPLAY_REGS]


# ─── Majority decode across a sample window ────────────────────────────────

# Fields voted on independently. `warnings` is excluded — it is diagnostic
# text, not a reading, and is unioned instead.
_VOTED_FIELDS = (
    "battery_soc",
    "dc_active",
    "ac_active",
    "output_mode",
    "output_watts",
    "time_to_empty_minutes",
    "time_to_full_minutes",
    "charging",
    "system_mode",
)


def decode_frames_majority(frames: list[list[int]]) -> Optional[DecodedFrame]:
    """Decode every frame captured in one sample window and return the
    per-field majority value.

    The tap is passive and bit-banged, and loses or corrupts a meaningful
    share of what it sees. Publishing the single newest frame — which is what
    this module's caller did until 2026-08-18 — hands that corruption straight
    through to the app: UNIT-002 showed SoC alternating 0/6/null and the AC
    outlet apparently switching itself on and off every fifteen seconds while
    nobody was touching it.

    Corruption here is random per byte rather than systematic, so the same
    field decoded across ~15-20 frames of one window agrees on the true value
    far more often than on any particular wrong one. Voting per field rather
    than per frame matters: a frame is rarely corrupt as a whole, so discarding
    whole frames would throw away good bytes alongside bad ones.

    A field with no non-None reading in the entire window stays None, which is
    the honest answer — better a blank than a number nobody should trust.
    Returns None only when handed nothing at all.
    """
    if not frames:
        return None

    decoded = [decode_frame(f) for f in frames]

    out: dict = {}
    for field in _VOTED_FIELDS:
        values = [d[field] for d in decoded if d[field] is not None]
        if not values:
            out[field] = None
            continue
        # Counter.most_common breaks ties by insertion order, i.e. by the
        # earliest frame in the window — deterministic rather than arbitrary.
        out[field] = Counter(values).most_common(1)[0][0]

    seen: list[str] = []
    for d in decoded:
        for w in d["warnings"]:
            if w not in seen:
                seen.append(w)
    out["warnings"] = seen

    return out  # type: ignore[return-value]
