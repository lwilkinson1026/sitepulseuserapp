"""
Predator LCD calibration capture rig.

Purpose
-------
Grow the decoder's lookup tables (`_BATTERY_SOC_LOOKUP` above all, but also
the watts/time digit tables and the AC/charging flag bits) by pairing live
I²C register snapshots against camera ground truth from the LCD itself.

The naive approach — photograph the LCD on a timer — produces thousands of
near-identical frames, because the LCD only refreshes at ~5.7 Hz and the
interesting bytes change on the order of minutes.  A 90-minute discharge is
~30,000 snapshots but only ~40 *distinct* battery states.

So this is **change-triggered**: it watches the bus continuously and only
burns a photograph when the key it's tracking takes on a value it has never
seen before.  A long discharge collapses into a few dozen rows, each one a
cropped LCD image sitting next to the exact register bytes that produced it.

That makes the session unattended.  Start it, apply a load, walk away.

Usage
-----
Capture (the discharge campaign — keys on the SoC byte pair)::

    python3 lcd_calibrate.py

Capture (the state-matrix session — keys on the flag/label registers, so
every AC/DC/charging toggle lands as its own row)::

    python3 lcd_calibrate.py --key flags

Build a labeled contact sheet from a finished session::

    python3 lcd_calibrate.py --sheet ~/lcd_calib/20260728-140312

Output
------
Each run creates ``~/lcd_calib/<timestamp>/`` containing:

    manifest.jsonl   one JSON row per captured state
    shots/NNN.png    the cropped LCD image for that row
    contact.png      (after --sheet) all crops montaged with labels

A manifest row looks like::

    {"idx": 3, "ts": "2026-07-28T14:11:52", "key_mode": "soc",
     "key": "EF,EF", "regs_hex": "EF EF 80 ...", "decoded": {...},
     "image": "shots/003.png"}

Camera note
-----------
The USB camera's exposure settings are session-only — they revert on reboot
and on USB re-plug.  Auto-exposure meters the whole dark room and blows out
the emissive LCD into an unreadable white blob, so we re-assert manual
exposure at startup on every run rather than assuming it stuck.

Wiring note
-----------
Requires the Predator I²C tap on GPIO 22 (SDA) / GPIO 23 (SCL) with a shared
ground — see ``predator_i2c_sniffer.py`` for the harness colors and the
warning about power rails.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from predator_decoder import FrameAssembler, decode_frame
from predator_i2c_sniffer import WATCH_ADDRESS, PassiveI2cSniffer

# ─── configuration ─────────────────────────────────────────────────────────

VIDEO_DEV = os.environ.get("SITEPULSE_LCD_VIDEO_DEV", "/dev/video0")
VIDEO_SIZE = os.environ.get("SITEPULSE_LCD_VIDEO_SIZE", "2320x1744")

# Exposure that renders the blue emissive segments legibly in a dark room.
# Derived empirically; auto-exposure does not work here.
EXPOSURE_ABS = int(os.environ.get("SITEPULSE_LCD_EXPOSURE", "800"))
EXPOSURE_GAIN = int(os.environ.get("SITEPULSE_LCD_GAIN", "40"))

# Crop rectangle isolating the LCD from the full camera frame: w:h:x:y.
# Re-derive this if the camera or generator is physically moved — see
# `--probe`, which locates the blue emissive segments and prints a rectangle.
#
# Deliberately wide enough to include the DC button (left) and AC button
# (right) along with their green LEDs.  Those LEDs are *independent* ground
# truth for exactly the states the register decode cannot currently tell
# apart — `ac_active` is presently inferred as "output on and not DC", and
# charging isn't detectable at all.  Photographing the display alone would
# leave both gaps unresolvable.
CROP = os.environ.get("SITEPULSE_LCD_CROP", "800:210:700:690")
CROP_SCALE = int(os.environ.get("SITEPULSE_LCD_CROP_SCALE", "3"))

# ─── AC outlet icon ────────────────────────────────────────────────────────
#
# The LCD carries a small *orange* NEMA-outlet glyph immediately right of the
# "%" sign, which the operator reports lights when the AC inverter is on.  It
# is the only visible element that distinguishes AC-on from AC-off, so it is
# our ground truth for the `ac_active` gap.
#
# It cannot be found with the blue mask used by `--probe`: that mask is
# (B > 40) & (B - R > 12), i.e. blue-dominant *by construction*, so an orange
# glyph is not merely missed but actively excluded.  We score it with the
# inverse — an R-over-B mask — and subtract a background patch sampled from
# the dead LCD area immediately to its right, which cancels ambient warm room
# light and sensor noise that would otherwise swamp a glyph this small.
#
# ROI is in *stored image* coordinates, i.e. after CROP and CROP_SCALE. It is
# therefore only valid for the default CROP; re-derive both together.
#
# Measured on UNIT-002 2026-07-28: lit ≈ 10-12, dark ≈ 4.4.  Threshold at 7.
AC_ICON_ROI = tuple(int(v) for v in os.environ.get(
    "SITEPULSE_LCD_AC_ICON_ROI", "1000:1120:415:515").split(":"))
AC_ICON_BG_X = tuple(int(v) for v in os.environ.get(
    "SITEPULSE_LCD_AC_ICON_BG", "1150:1270").split(":"))
AC_ICON_THRESHOLD = float(os.environ.get("SITEPULSE_LCD_AC_ICON_THRESHOLD", "7.0"))


def score_ac_icon(image: Path) -> Optional[float]:
    """Return the orange-icon score for a captured shot, or None if it can't
    be measured (missing numpy/PIL, unreadable file, ROI outside the image).

    Deliberately soft-failing: this is an *observability* addition to a
    capture rig whose primary job is photographing the LCD, and a missing
    numpy on some future host must not cost us the session.
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    x0, x1, y0, y1 = AC_ICON_ROI
    bx0, bx1 = AC_ICON_BG_X
    try:
        arr = np.asarray(Image.open(image).convert("RGB"))
    except Exception:
        return None
    if arr.shape[0] < y1 or arr.shape[1] < max(x1, bx1):
        return None
    orange = np.clip(arr[:, :, 0].astype(np.int16) - arr[:, :, 2].astype(np.int16), 0, None)
    return round(float(orange[y0:y1, x0:x1].mean() - orange[y0:y1, bx0:bx1].mean()), 2)

SESSION_ROOT = Path(os.environ.get("SITEPULSE_LCD_CALIB_DIR", "~/lcd_calib")).expanduser()

# Registers that change constantly during normal operation (watts + time
# digits).  Excluded from the "flags" key so that a load fluctuating by a few
# watts doesn't trigger a photograph on every single frame.
_VOLATILE_REGS = {0x02, 0x03, 0x04, 0x0B, 0x0C, 0x0D, 0x0E, 0x11}


# ─── key extraction ────────────────────────────────────────────────────────

def _key_soc(regs: list[int]) -> str:
    """Battery SoC byte pair — the primary calibration target."""
    return f"{regs[0x00]:02X},{regs[0x01]:02X}"


# The watts and time values live in the low 7 bits of these registers as
# 7-segment digit patterns; bit 7 of each is an independent indicator flag
# (0x11 = DC, 0x02 = "thousands active", 0x0C = the time colon, and the rest
# are as yet unidentified — prime candidates for an AC or charging bit).
_DIGIT_REGS = (0x02, 0x03, 0x04, 0x11, 0x0B, 0x0C, 0x0D, 0x0E)
_FLAG_REGS = (0x00, 0x01, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0F, 0x10)


def _key_flags(regs: list[int]) -> str:
    """State key for the attended state-matrix session.

    An earlier version excluded the watts/time registers wholesale to keep a
    fluctuating load from triggering a photo on every frame.  That was wrong,
    and wrong in the one way that mattered: a 1000 W AC load applied while DC
    is also enabled changes *only* the watts digits, so the key never moved
    and the session recorded nothing.  Live-verified on UNIT-002.

    So we keep the noise suppression but stop discarding the signal:

      * flag/label registers verbatim
      * the high *bit* of each digit register — those are indicator flags,
        not part of the digit, and they are where an AC or charging bit
        would most plausibly live
      * watts bucketed to the nearest 100 W, so 0 W vs 1000 W is a distinct
        state but a load jittering by a few watts is not
    """
    parts = [f"{regs[r]:02X}" for r in _FLAG_REGS]

    hi = 0
    for i, r in enumerate(_DIGIT_REGS):
        if regs[r] & 0x80:
            hi |= 1 << i
    parts.append(f"HI{hi:02X}")

    watts = decode_frame(regs)["output_watts"]
    parts.append("W??" if watts is None else f"W{int(watts) // 100:02d}")
    return ",".join(parts)


def _key_full(regs: list[int]) -> str:
    """Every register.  Captures a lot; use for short focused sessions."""
    return ",".join(f"{b:02X}" for b in regs)


KEY_MODES = {"soc": _key_soc, "flags": _key_flags, "full": _key_full}


# ─── camera ────────────────────────────────────────────────────────────────

def configure_camera() -> None:
    """Force manual exposure.  These controls are session-only and silently
    revert on reboot or USB re-plug, so we always re-assert rather than
    trusting that a previous run's settings survived."""
    cmds = [
        ["v4l2-ctl", "-d", VIDEO_DEV, "--set-ctrl", "auto_exposure=1"],
        ["v4l2-ctl", "-d", VIDEO_DEV,
         "--set-ctrl", f"exposure_time_absolute={EXPOSURE_ABS}",
         "--set-ctrl", f"gain={EXPOSURE_GAIN}"],
    ]
    for cmd in cmds:
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"[calib] WARNING: {' '.join(cmd)} failed: {res.stderr.strip()}",
                  file=sys.stderr)
    # auto_exposure must be set to manual *before* exposure_time_absolute
    # becomes writable, which is why the two calls above are ordered and
    # separate rather than combined.


def grab_lcd(dest: Path, keep_raw: bool = False) -> bool:
    """Capture one frame and write the cropped, upscaled LCD region to dest.
    Returns True on success.  Never raises — a failed photograph should not
    kill an unattended multi-hour session.

    The uncropped frame is deleted unless `keep_raw`, which is worth setting
    if the crop rectangle might be wrong — otherwise a mis-aimed camera
    silently produces a session of blank crops with no way to recover the
    underlying frames."""
    raw = dest.with_suffix(".raw.jpg")
    try:
        grab = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "v4l2", "-input_format", "mjpeg",
             "-video_size", VIDEO_SIZE, "-i", VIDEO_DEV,
             "-frames:v", "1", "-q:v", "2", "-y", str(raw)],
            capture_output=True, text=True, timeout=20,
        )
        if grab.returncode != 0:
            print(f"[calib] camera grab failed: {grab.stderr.strip()}", file=sys.stderr)
            return False

        crop = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(raw),
             "-vf", f"crop={CROP},scale=iw*{CROP_SCALE}:ih*{CROP_SCALE}:flags=lanczos",
             "-y", str(dest)],
            capture_output=True, text=True, timeout=20,
        )
        if crop.returncode != 0:
            print(f"[calib] crop failed: {crop.stderr.strip()}", file=sys.stderr)
            return False
        return True
    except subprocess.TimeoutExpired:
        print("[calib] camera timed out", file=sys.stderr)
        return False
    finally:
        if not keep_raw:
            raw.unlink(missing_ok=True)


def probe_crop(margin: int) -> None:
    """Locate the LCD in the camera frame and print a CROP rectangle.

    The LCD's segments are the only strongly blue-dominant thing in a dark
    room, so masking on "blue clearly exceeds red" isolates it from the
    generator body and from warm room light.  We then take the *densest*
    connected band rather than the outright min/max, because stray blue
    reflections elsewhere in the room (and any other glowing electronics on
    the bench) would otherwise stretch the box to uselessness.

    Run this after moving the camera or the generator.
    """
    from PIL import Image
    import numpy as np

    configure_camera()
    raw = Path("/tmp/lcd_probe.jpg")
    grab = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "v4l2", "-input_format", "mjpeg",
         "-video_size", VIDEO_SIZE, "-i", VIDEO_DEV,
         "-frames:v", "1", "-q:v", "2", "-y", str(raw)],
        capture_output=True, text=True, timeout=20,
    )
    if grab.returncode != 0:
        print(f"[probe] camera grab failed: {grab.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    a = np.asarray(Image.open(raw).convert("RGB")).astype(int)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    mask = (B > 40) & (B - R > 12)
    total = int(mask.sum())
    print(f"[probe] frame {a.shape[1]}x{a.shape[0]}, {total} blue-ish px")
    if total < 200:
        print("[probe] too few blue pixels — is the LCD awake and the room dark?",
              file=sys.stderr)
        sys.exit(1)

    def densest_band(profile: np.ndarray, gap: int) -> tuple[int, int]:
        """Grow a band outward from the single densest bin.

        Anchoring on the peak matters: the obvious implementation — take the
        longest run of non-empty bins — chains the LCD to any *other* blue
        light source on the bench through the dark bins between them, and
        returns the entire frame.  The LCD is by a wide margin the densest
        blue cluster, so we start there and stop once `gap` consecutive bins
        fall below a small fraction of the peak.
        """
        if profile.max() <= 0:
            return 0, len(profile) - 1
        peak = int(profile.argmax())
        floor = profile[peak] * 0.04

        lo = peak
        run = 0
        for i in range(peak - 1, -1, -1):
            if profile[i] > floor:
                lo, run = i, 0
            else:
                run += 1
                if run > gap:
                    break
        hi = peak
        run = 0
        for i in range(peak + 1, len(profile)):
            if profile[i] > floor:
                hi, run = i, 0
            else:
                run += 1
                if run > gap:
                    break
        return lo, hi

    bin_px = 25
    colp = np.array([mask[:, i:i + bin_px].sum()
                     for i in range(0, mask.shape[1], bin_px)])
    rowp = np.array([mask[i:i + bin_px, :].sum()
                     for i in range(0, mask.shape[0], bin_px)])
    # A 6-bin (150 px) gap tolerance spans the dark space between the SoC
    # digits and the watts group without leaping to unrelated light sources.
    c0, c1 = densest_band(colp, gap=6)
    r0, r1 = densest_band(rowp, gap=6)

    x = max(0, c0 * bin_px - margin)
    y = max(0, r0 * bin_px - margin)
    w = min(a.shape[1] - x, (c1 + 1) * bin_px + margin - x)
    h = min(a.shape[0] - y, (r1 + 1) * bin_px + margin - y)

    print(f"[probe] suggested CROP = {w}:{h}:{x}:{y}")
    print(f"[probe] preview → /tmp/lcd_probe_crop.png")
    print(f"[probe] apply with:  export SITEPULSE_LCD_CROP={w}:{h}:{x}:{y}")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(raw),
         "-vf", f"crop={w}:{h}:{x}:{y},scale=iw*2:ih*2:flags=lanczos",
         "-y", "/tmp/lcd_probe_crop.png"],
        capture_output=True, text=True, timeout=20,
    )
    print("[probe] check the preview covers the whole display before "
          "committing it to CROP.")


# ─── capture session ───────────────────────────────────────────────────────

def run_capture(key_mode: str, stable_frames: int, max_shots: int,
                keep_raw: bool) -> None:
    keyfn = KEY_MODES[key_mode]

    session = SESSION_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    shots = session / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    manifest = session / "manifest.jsonl"

    print(f"[calib] session   : {session}")
    print(f"[calib] key mode  : {key_mode}")
    print(f"[calib] watching  : 0x{WATCH_ADDRESS:02X}")
    print(f"[calib] stability : {stable_frames} frames")
    print("[calib] configuring camera...")
    configure_camera()

    seen: set[str] = set()
    idx = 0
    frames = 0
    pending_key: Optional[str] = None
    pending_count = 0
    last_report = time.monotonic()

    assembler = FrameAssembler()

    print("[calib] running — Ctrl-C to stop\n")
    try:
        with PassiveI2cSniffer() as sniff:
            for txn in sniff.transactions(timeout_s=2.0):
                if txn is None:
                    continue
                if txn.address != WATCH_ADDRESS or len(txn.data) != 3:
                    continue
                if txn.data[0] != 0x80:
                    continue

                regs = assembler.feed(txn.data[1], txn.data[2])
                if regs is None:
                    continue

                frames += 1
                key = keyfn(regs)

                # Debounce: the key must hold steady for several consecutive
                # frames before we trust it.  Without this we photograph
                # transient states the LCD paints mid-update.
                if key == pending_key:
                    pending_count += 1
                else:
                    pending_key = key
                    pending_count = 1

                if pending_count != stable_frames or key in seen:
                    # Periodic heartbeat so an unattended run shows progress.
                    now = time.monotonic()
                    if now - last_report >= 30:
                        decoded = decode_frame(regs)
                        soc = decoded["battery_soc"]
                        print(f"[calib] {frames:6d} frames  {len(seen):3d} states  "
                              f"now={key}  soc={soc if soc is not None else '?'}")
                        last_report = now
                    continue

                # New, stable state → photograph it.
                seen.add(key)
                idx += 1
                decoded = decode_frame(regs)
                image = shots / f"{idx:03d}.png"

                ok = grab_lcd(image, keep_raw=keep_raw)

                # Ground truth for `ac_active`, measured off the photograph we
                # just took rather than inferred from the registers — the whole
                # point is to have something independent to correlate against.
                icon = score_ac_icon(image) if ok else None

                row = {
                    "idx": idx,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "key_mode": key_mode,
                    "key": key,
                    "regs_hex": " ".join(f"{b:02X}" for b in regs),
                    "decoded": {k: v for k, v in decoded.items() if k != "warnings"},
                    "warnings": decoded["warnings"],
                    "image": str(image.relative_to(session)) if ok else None,
                    "ac_icon_score": icon,
                    "ac_icon_lit": None if icon is None else icon >= AC_ICON_THRESHOLD,
                }
                with manifest.open("a") as fh:
                    fh.write(json.dumps(row) + "\n")

                soc = decoded["battery_soc"]
                flag = "KNOWN" if soc is not None else " NEW "
                print(f"[calib] [{flag}] #{idx:03d}  key={key}  "
                      f"soc={soc if soc is not None else '??'}  "
                      f"watts={decoded['output_watts']}  "
                      f"mode={decoded['output_mode']}  "
                      f"ac_icon={'?' if icon is None else ('LIT' if icon >= AC_ICON_THRESHOLD else 'dark')}"
                      f"{'' if icon is None else f' ({icon:.1f})'}"
                      f"{'' if ok else '  (PHOTO FAILED)'}")

                # The photograph blocks for a second or two, during which the
                # sniffer queue backs up with stale transactions.  Drain it and
                # restart frame assembly so we resume on a clean boundary.
                drained = 0
                for stale in sniff.transactions(timeout_s=0.0):
                    if stale is None:
                        break
                    drained += 1
                    if drained > 5000:
                        break
                assembler = FrameAssembler()
                pending_key, pending_count = None, 0

                if max_shots and idx >= max_shots:
                    print(f"[calib] reached --max-shots {max_shots}, stopping")
                    break

    except KeyboardInterrupt:
        print("\n[calib] stopped by user")

    print(f"\n[calib] {frames} frames observed, {len(seen)} distinct states captured")
    print(f"[calib] manifest: {manifest}")
    print(f"[calib] build a contact sheet with:")
    print(f"          python3 lcd_calibrate.py --sheet {session}")


# ─── contact sheet ─────────────────────────────────────────────────────────

def build_sheet(session: Path, cols: int, thumb_w: int) -> None:
    """Montage every captured crop into one labeled image.

    The point is review cost: 40 separate images means 40 separate looks,
    while one contact sheet with the register key printed under each cell can
    be read in a single pass.

    Cells are downscaled to `thumb_w`.  The shots on disk stay at full
    capture resolution for reading individual segments; without a separate
    thumbnail size a 40-state sheet comes out ~9000 px wide and every viewer
    downsamples it back into illegibility.
    """
    from PIL import Image, ImageDraw, ImageFont

    manifest = session / "manifest.jsonl"
    if not manifest.exists():
        print(f"[calib] no manifest at {manifest}", file=sys.stderr)
        sys.exit(1)

    rows = []
    for line in manifest.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("image"):
                # Re-decode from the stored raw bytes rather than trusting the
                # `decoded` blob frozen in at capture time.  The workflow is
                # iterative — you read the sheet, add rows to the lookup
                # tables, and rebuild — and that only converges if rebuilding
                # reflects the *current* decoder.  Amber cells are exactly the
                # states still needing a table entry.
                try:
                    regs = [int(b, 16) for b in row["regs_hex"].split()]
                    row["decoded"] = dict(decode_frame(regs))
                except (KeyError, ValueError):
                    pass  # fall back to the stored decode
                rows.append(row)

    if not rows:
        print("[calib] manifest has no captured images", file=sys.stderr)
        sys.exit(1)

    font = None
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"):
        try:
            font = ImageFont.truetype(path, 18)
            break
        except OSError:
            continue
    if font is None:
        # load_default() is tiny and unreadable at these sizes, but a sheet
        # with small labels beats no sheet at all.
        font = ImageFont.load_default()

    thumbs = []
    for row in rows:
        img = Image.open(session / row["image"]).convert("RGB")
        if img.width > thumb_w:
            h = max(1, round(img.height * thumb_w / img.width))
            img = img.resize((thumb_w, h), Image.LANCZOS)
        thumbs.append((row, img))

    cell_w = max(img.width for _, img in thumbs)
    cell_h = max(img.height for _, img in thumbs)
    label_h = 58
    pad = 10

    n = len(thumbs)
    cols = max(1, min(cols, n))
    grid_rows = (n + cols - 1) // cols

    sheet_w = cols * (cell_w + pad) + pad
    sheet_h = grid_rows * (cell_h + label_h + pad) + pad
    sheet = Image.new("RGB", (sheet_w, sheet_h), (18, 18, 18))
    draw = ImageDraw.Draw(sheet)

    def clamp(text: str, width: int) -> str:
        """Trim to fit the cell.  An overflowing label runs under the *next*
        image, where it reads as that image's caption — worse than truncation."""
        if draw.textlength(text, font=font) <= width:
            return text
        while text and draw.textlength(text + "…", font=font) > width:
            text = text[:-1]
        return text + "…"

    for i, (row, img) in enumerate(thumbs):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = pad + r * (cell_h + label_h + pad)
        sheet.paste(img, (x, y))

        soc = row["decoded"].get("battery_soc")
        line1 = f"#{row['idx']:03d}  {row['key']}"
        line2 = (f"soc={soc if soc is not None else '??'}  "
                 f"W={row['decoded'].get('output_watts')}  "
                 f"{row['decoded'].get('output_mode')}")
        colour = (120, 220, 120) if soc is not None else (240, 190, 90)
        draw.text((x + 4, y + cell_h + 4), clamp(line1, cell_w - 8),
                  fill=colour, font=font)
        draw.text((x + 4, y + cell_h + 28), clamp(line2, cell_w - 8),
                  fill=(190, 190, 190), font=font)

    out = session / "contact.png"
    sheet.save(out)
    print(f"[calib] wrote {out}  ({n} states, {cols}x{grid_rows})")


# ─── entrypoint ────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Predator LCD calibration capture")
    ap.add_argument("--key", choices=sorted(KEY_MODES), default="soc",
                    help="what counts as a distinct state (default: soc)")
    ap.add_argument("--stable", type=int, default=3,
                    help="frames a key must hold before capture (default: 3)")
    ap.add_argument("--max-shots", type=int, default=0,
                    help="stop after N captures (0 = unlimited)")
    ap.add_argument("--keep-raw", action="store_true",
                    help="keep the uncropped camera frames")
    ap.add_argument("--sheet", metavar="SESSION_DIR",
                    help="build a contact sheet from a finished session")
    ap.add_argument("--cols", type=int, default=4,
                    help="contact sheet columns (default: 4)")
    ap.add_argument("--thumb-width", type=int, default=620,
                    help="contact sheet cell width in px (default: 620)")
    ap.add_argument("--probe", action="store_true",
                    help="locate the LCD and print a CROP rectangle, then exit")
    ap.add_argument("--probe-margin", type=int, default=30,
                    help="px of padding around the detected LCD (default: 30)")
    args = ap.parse_args()

    if args.probe:
        probe_crop(args.probe_margin)
        return

    if args.sheet:
        build_sheet(Path(args.sheet).expanduser(), args.cols, args.thumb_width)
        return

    run_capture(args.key, args.stable, args.max_shots, args.keep_raw)


if __name__ == "__main__":
    main()
