"""
SitePulse live stream — ffmpeg push from USB webcam to Cloudflare Stream.

When the app issues a `camera.startStream` command:
  - We check that no stream is already running.
  - We check that sentry isn't holding the camera (configurable: either
    auto-pause it or refuse). v1 refuses with a clear error in
    current/camera.error so the app can prompt the user to disarm.
  - We spawn ffmpeg as a subprocess that captures /dev/video0 and pushes
    over RTMPS to Cloudflare Stream Live.
  - We mirror state to units/{u}/current/camera so the app's VideoView
    knows which HLS URL to play.

When the app issues `camera.stopStream`:
  - We send SIGTERM to ffmpeg, wait briefly, SIGKILL if needed.
  - We mirror `streaming: false`, clear the HLS URL.

The Cloudflare Stream Live Input is provisioned out-of-band (Cloudflare
dashboard). The Pi gets two env vars:
    SITEPULSE_CLOUDFLARE_RTMP_TARGET
        Full RTMPS push URL including the secret stream key, e.g.
        rtmps://live.cloudflare.com:443/live/abc123...
    SITEPULSE_CLOUDFLARE_HLS_URL
        Full HLS playback URL, e.g.
        https://customer-XYZ.cloudflarestream.com/UID/manifest/video.m3u8

Both env vars stay on the Pi — the secret RTMP key is never written to
Firestore or seen by the client.

Streaming/sentry coordination:
    Both want /dev/video0. v1 makes them mutually exclusive. If sentry
    is armed when the user taps "Go Live", we write a clear error to
    current/camera.error: "DISARM SENTRY TO GO LIVE" and refuse to
    start. Future improvement: coordinated pause via a shared lock.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from typing import Any, Dict, Optional

from firebase_admin import firestore


CAMERA_DEVICE = os.environ.get("SITEPULSE_CAMERA_DEVICE", "/dev/video0")
RTMP_TARGET   = os.environ.get("SITEPULSE_CLOUDFLARE_RTMP_TARGET", "")
HLS_URL       = os.environ.get("SITEPULSE_CLOUDFLARE_HLS_URL", "")

# Bitrate + resolution for the live push. Conservative for Starlink upload.
STREAM_W       = int(os.environ.get("SITEPULSE_STREAM_W", "1280"))
STREAM_H       = int(os.environ.get("SITEPULSE_STREAM_H", "720"))
STREAM_FPS     = int(os.environ.get("SITEPULSE_STREAM_FPS", "24"))
STREAM_BITRATE = os.environ.get("SITEPULSE_STREAM_BITRATE", "1500k")

# Hard cap so a forgotten stream doesn't drain Cloudflare credits overnight.
STREAM_MAX_DURATION_S = int(os.environ.get("SITEPULSE_STREAM_MAX_S", str(60 * 30)))


# ─── process state ──────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_ffmpeg_proc: Optional[subprocess.Popen] = None
_started_at: Optional[float] = None
_watchdog_thread: Optional[threading.Thread] = None


def _is_running() -> bool:
    proc = _ffmpeg_proc
    return proc is not None and proc.poll() is None


def _set_current(db: firestore.Client, unit_id: str, patch: Dict[str, Any]) -> None:
    db.document(f"units/{unit_id}/current/camera").set(patch, merge=True)


def _build_ffmpeg_cmd() -> list[str]:
    """Compose the ffmpeg argv. h264_v4l2m2m if hardware encoder is present
    (Pi 5 has it), otherwise libx264 ultrafast. anullsrc fills the audio
    track because Cloudflare prefers a non-empty A stream."""
    # Hardware encoder probe: hard to do reliably without running it.
    # Caller can force via SITEPULSE_STREAM_ENCODER=libx264 to override.
    encoder = os.environ.get("SITEPULSE_STREAM_ENCODER", "libx264")
    enc_flags = (
        ["-c:v", "h264_v4l2m2m", "-b:v", STREAM_BITRATE]
        if encoder == "h264_v4l2m2m"
        else ["-c:v", "libx264",
              "-preset", "ultrafast",
              "-tune", "zerolatency",
              "-b:v", STREAM_BITRATE]
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-f", "v4l2",
        "-framerate", str(STREAM_FPS),
        "-video_size", f"{STREAM_W}x{STREAM_H}",
        "-i", CAMERA_DEVICE,
        # Silent audio track — keeps Cloudflare ingest happy.
        "-f", "lavfi",
        "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        *enc_flags,
        "-pix_fmt", "yuv420p",
        "-g", str(STREAM_FPS * 2),  # keyframe every 2s
        "-c:a", "aac",
        "-ar", "44100",
        "-b:a", "64k",
        "-ac", "2",
        "-shortest",
        "-f", "flv",
        RTMP_TARGET,
    ]


def _sentry_is_armed(db: firestore.Client, unit_id: str) -> bool:
    try:
        snap = db.document(f"units/{unit_id}/config/sentry").get()
        return bool(snap.exists and snap.get("enabled"))
    except Exception:
        return False


def _start_watchdog(db: firestore.Client, unit_id: str) -> None:
    """Auto-stop the stream after STREAM_MAX_DURATION_S as a credit guard."""
    global _watchdog_thread

    def watch():
        while _is_running():
            elapsed = time.monotonic() - (_started_at or time.monotonic())
            if elapsed >= STREAM_MAX_DURATION_S:
                print(
                    f"[streamer] hit max duration {STREAM_MAX_DURATION_S}s — stopping",
                    flush=True,
                )
                try:
                    handle_camera_stop(db, unit_id, {"reason": "max_duration"})
                except Exception as e:
                    print(f"[streamer] auto-stop failed: {e}", flush=True)
                return
            time.sleep(2)

    _watchdog_thread = threading.Thread(target=watch, daemon=True, name="stream-watchdog")
    _watchdog_thread.start()


# ─── command handlers ──────────────────────────────────────────────────────

def handle_camera_start(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """payload: {} — checks env, refuses on conflict, otherwise spawns ffmpeg."""
    global _ffmpeg_proc, _started_at

    if not RTMP_TARGET or not HLS_URL:
        _set_current(db, unit_id, {
            "streaming": False,
            "hlsUrl": None,
            "error": "CLOUDFLARE STREAM ENV NOT SET ON PI",
        })
        raise RuntimeError(
            "SITEPULSE_CLOUDFLARE_RTMP_TARGET and SITEPULSE_CLOUDFLARE_HLS_URL "
            "must be set in the Pi's environment"
        )

    with _state_lock:
        if _is_running():
            _set_current(db, unit_id, {
                "streaming": True,
                "hlsUrl": HLS_URL,
                "error": None,
            })
            return  # Idempotent: already streaming.

        if _sentry_is_armed(db, unit_id):
            _set_current(db, unit_id, {
                "streaming": False,
                "hlsUrl": None,
                "error": "DISARM SENTRY TO GO LIVE",
            })
            raise RuntimeError("camera busy — disarm sentry first")

        cmd = _build_ffmpeg_cmd()
        print(f"[streamer] starting ffmpeg: {shlex.join(cmd[:-1])} <RTMP_TARGET>", flush=True)
        try:
            _ffmpeg_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            _set_current(db, unit_id, {
                "streaming": False,
                "hlsUrl": None,
                "error": "FFMPEG NOT INSTALLED ON PI",
            })
            raise RuntimeError("ffmpeg binary not found on Pi") from e

        _started_at = time.monotonic()

    # Give ffmpeg ~2s to actually establish the RTMP connection. If it crashes
    # immediately (e.g. wrong stream key), report the failure.
    time.sleep(2)
    if not _is_running():
        proc = _ffmpeg_proc
        tail = ""
        try:
            if proc and proc.stdout:
                tail = proc.stdout.read().decode("utf-8", errors="replace")[-400:]
        except Exception:
            pass
        _set_current(db, unit_id, {
            "streaming": False,
            "hlsUrl": None,
            "error": f"FFMPEG EXITED EARLY  ·  {tail.strip()[:200]}",
        })
        raise RuntimeError(f"ffmpeg exited before stream stabilized; tail={tail!r}")

    _set_current(db, unit_id, {
        "streaming": True,
        "hlsUrl": HLS_URL,
        "startedAt": firestore.SERVER_TIMESTAMP,
        "error": None,
    })
    db.collection(f"units/{unit_id}/events").add({
        "kind": "system.online",  # reuse existing kind so push fan-out skips it
        "at": firestore.SERVER_TIMESTAMP,
        "source": "pi",
        "payload": {"action": "camera.startStream"},
    })
    _start_watchdog(db, unit_id)
    print("[streamer] live", flush=True)


def handle_camera_stop(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """payload: {} — graceful SIGTERM then SIGKILL after 3s."""
    global _ffmpeg_proc, _started_at

    with _state_lock:
        proc = _ffmpeg_proc
        if proc is None or proc.poll() is not None:
            _set_current(db, unit_id, {
                "streaming": False,
                "hlsUrl": None,
                "error": None,
            })
            _ffmpeg_proc = None
            _started_at = None
            return

        try:
            # Kill the whole process group so child threads inside ffmpeg die too.
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    # Wait briefly for graceful shutdown, then force.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass

    with _state_lock:
        _ffmpeg_proc = None
        _started_at = None

    _set_current(db, unit_id, {
        "streaming": False,
        "hlsUrl": None,
        "stoppedAt": firestore.SERVER_TIMESTAMP,
        "error": None,
    })
    print("[streamer] stopped", flush=True)


# ─── lifecycle hook (called by command_listener.py at boot) ────────────────

def start_streamer(_db: firestore.Client, _unit_id: str) -> threading.Thread:
    """No background loop needed — the streamer is purely reactive to
    `camera.startStream` / `camera.stopStream` commands. We return a
    placeholder thread so the listener's start-services interface stays
    uniform with relays / scheduler / sentry."""
    placeholder = threading.Thread(target=lambda: None, daemon=True, name="streamer-idle")
    placeholder.start()
    return placeholder
