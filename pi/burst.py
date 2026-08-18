"""Cross-process "publish faster for a moment" signal.

The command listener and the telemetry publisher are separate systemd
services, so a user pressing "Wake LCD" in the app has no in-process way to
tell the publisher that something worth watching is about to happen.  Without
this, the press lands, the panel lights up, and the app then waits out
whatever remains of the 15 s publish interval — up to 15 s of staring at a
stale `battery_soc: null` while the data is already on the bus.

A burst is deliberately NOT a Firestore document.  The only reason the
publish interval is 15 s in the first place is that Firestore writes are the
scarce resource here (Spark free tier, 20,000 writes/day shared across the
whole fleet — see the header comment in firebase_publisher.py).  Spending
writes to coordinate a feature whose entire purpose is to spend fewer of
them would be self-defeating.  A local file costs nothing, and both services
already run as the same user on the same box.

Wall-clock time is used rather than time.monotonic() because two processes
do not share a monotonic epoch.  That makes this sensitive to clock steps,
and this Pi does take an NTP correction of several minutes shortly after
boot — so a stamp that sits in the future, or further in the past than the
window, is treated as expired rather than trusted.  The failure mode is a
burst that ends early, never one that pins the publisher at high cadence.
"""

from __future__ import annotations

import os
import time
from typing import Optional, Tuple

# Both services run as the same user, so the default lives beside the rest of
# the runtime.  Overridable for the /opt/sitepulse fleet layout, where the
# service user's home is not ~sitepulse2.
BURST_FILE = os.path.expanduser(
    os.environ.get("SITEPULSE_BURST_FILE", "~/sitepulse/.wake_burst")
)

# How long a single press stays "interesting".  The Predator LCD sleeps on
# its own timer, so there is no point bursting much past the point where the
# panel goes dark again.
WINDOW_S = float(os.environ.get("SITEPULSE_BURST_WINDOW", "90"))

# Cadence while bursting.  90 s at 3 s is 30 writes where the 15 s baseline
# would have spent 6 — a marginal cost of 24 writes per press, against ~6,400
# writes/day of headroom.  Even fifty presses a day is under 20% of that.
INTERVAL_S = float(os.environ.get("SITEPULSE_BURST_INTERVAL", "3"))


def request(reason: str = "") -> None:
    """Ask the publisher to raise its cadence for WINDOW_S.

    Never raises: a burst is an optimisation, and failing to write the
    sentinel must not take down the servo press that triggered it.
    """
    tmp = f"{BURST_FILE}.tmp"
    try:
        parent = os.path.dirname(BURST_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w") as fh:
            fh.write(f"{time.time():.3f} {reason}\n")
        # Atomic swap so a reader mid-write never sees a truncated stamp.
        os.replace(tmp, BURST_FILE)
    except OSError as e:
        print(f"[burst] could not signal ({e})", flush=True)


def _stamp() -> Optional[float]:
    try:
        with open(BURST_FILE) as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def state() -> Tuple[float, float]:
    """Return (token, seconds_remaining).

    `token` is the raw request timestamp and changes on every new press, so a
    caller can distinguish "still in the same burst" from "a fresh press just
    landed" and publish immediately for the latter.  It is 0.0 when no burst
    has ever been requested.
    """
    ts = _stamp()
    if ts is None:
        return (0.0, 0.0)

    age = time.time() - ts
    # age < 0 means the stamp is in the future, which on this Pi means the
    # clock stepped rather than that someone pressed a button in the future.
    if age < 0 or age >= WINDOW_S:
        return (ts, 0.0)
    return (ts, WINDOW_S - age)
