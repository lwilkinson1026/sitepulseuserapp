"""
SitePulse sentry — USB-webcam motion detection + clip recording.

When armed (via `sentry.arm` command or config/sentry.enabled = true):
  - Reads frames from /dev/video0 via OpenCV.
  - Background-subtraction motion detector at ~5 fps to keep CPU low.
  - Maintains a `recordPreSec`-second rolling buffer in RAM.
  - On motion event: writes `recordPreSec + recordPostSec` of MP4 to
    Firebase Storage at units/{UNIT_ID}/clips/{epoch_ms}.mp4, drops a
    JPEG thumbnail next to it, and appends to units/{UNIT_ID}/events
    with kind='motion' and clipPath + thumbnailPath set. Cloud Function
    onEventCreated picks it up and fans out an Expo Push notification.
  - Cooldown of 30s between consecutive motion events.

Handlers (imported lazily by command_listener.py):
    handle_sentry_arm(db, unit_id, payload)
    handle_sentry_disarm(db, unit_id, payload)
    handle_sentry_update(db, unit_id, payload)

Plus a one-shot launcher:
    start_sentry(db, unit_id)

The launcher subscribes to config/sentry — when `enabled` flips true, the
capture thread starts; when it flips false, the thread exits. So the
state machine is: command writes config → snapshot listener reacts.

Hardware:
    USB webcam (UVC class). Default device /dev/video0.
    `pip3 install opencv-python-headless` on the Pi. `opencv-python`
    (with GUI bits) also works but pulls in Qt and is ~2x larger.

Env:
    SITEPULSE_CAMERA_DEVICE   default /dev/video0
    SITEPULSE_CAMERA_FPS      default 5  (capture+analysis rate)
    SITEPULSE_CAMERA_W        default 640
    SITEPULSE_CAMERA_H        default 480
    SITEPULSE_SENTRY_COOLDOWN default 30  (seconds between events)
"""

from __future__ import annotations

import collections
import os
import tempfile
import threading
import time
from typing import Any, Deque, Dict, List, Optional, Tuple

import firebase_admin
from firebase_admin import firestore, storage


CAMERA_DEVICE     = os.environ.get("SITEPULSE_CAMERA_DEVICE", "/dev/video0")
CAMERA_FPS        = int(os.environ.get("SITEPULSE_CAMERA_FPS", "5"))
CAMERA_W          = int(os.environ.get("SITEPULSE_CAMERA_W", "640"))
CAMERA_H          = int(os.environ.get("SITEPULSE_CAMERA_H", "480"))
SENTRY_COOLDOWN_S = float(os.environ.get("SITEPULSE_SENTRY_COOLDOWN", "30"))


# ─── lazy OpenCV import ─────────────────────────────────────────────────────
# Lets this file import on a Mac (no cv2) so the listener can still boot.

_CV = None
_CV_IMPORT_ERR: Optional[str] = None

def _cv():
    global _CV, _CV_IMPORT_ERR
    if _CV is not None:
        return _CV
    if _CV_IMPORT_ERR is not None:
        raise RuntimeError(f"opencv-python unavailable: {_CV_IMPORT_ERR}")
    try:
        import cv2   # type: ignore[import-not-found]
        _CV = cv2
        return _CV
    except Exception as e:
        _CV_IMPORT_ERR = str(e)
        raise RuntimeError(f"opencv-python unavailable: {e}") from e


# ─── state ──────────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_capture_thread: Optional[threading.Thread] = None
_stop_flag = threading.Event()
_last_motion_at: float = 0.0


# ─── command handlers (called by command_listener.py) ──────────────────────

def handle_sentry_arm(db: firestore.Client, unit_id: str, _payload: Dict[str, Any]) -> None:
    """payload: {} — just flips config/sentry.enabled = true."""
    db.document(f"units/{unit_id}/config/sentry").set({"enabled": True}, merge=True)
    db.collection(f"units/{unit_id}/events").add({
        "kind": "sentry.armed",
        "at": firestore.SERVER_TIMESTAMP,
        "source": "app",
    })


def handle_sentry_disarm(db: firestore.Client, unit_id: str, _payload: Dict[str, Any]) -> None:
    db.document(f"units/{unit_id}/config/sentry").set({"enabled": False}, merge=True)
    db.collection(f"units/{unit_id}/events").add({
        "kind": "sentry.disarmed",
        "at": firestore.SERVER_TIMESTAMP,
        "source": "app",
    })


def handle_sentry_update(db: firestore.Client, unit_id: str, payload: Dict[str, Any]) -> None:
    """payload = {configPatch: Partial<SentryConfig>}"""
    patch = payload.get("configPatch") or {}
    if not isinstance(patch, dict):
        raise ValueError("sentry.update: configPatch must be an object")
    db.document(f"units/{unit_id}/config/sentry").set(patch, merge=True)


# ─── capture + motion detection loop ────────────────────────────────────────

def _bucket():
    """Return the default Firebase Storage bucket. Initialized once."""
    try:
        return storage.bucket()
    except ValueError:
        # initialize_app called without a storageBucket option — re-init.
        app = firebase_admin.get_app()
        project_id = app.project_id
        # Default bucket naming convention used by Firebase Storage.
        bucket_name = f"{project_id}.firebasestorage.app"
        return storage.bucket(name=bucket_name, app=app)


def _capture_loop(db: firestore.Client, unit_id: str, config: Dict[str, Any]) -> None:
    """Read frames, detect motion, record + upload clips. Returns when
    _stop_flag is set."""
    global _last_motion_at
    cv = _cv()

    pre_sec  = int(config.get("recordPreSec", 5))
    post_sec = int(config.get("recordPostSec", 10))
    # sensitivity in [0,1]: lower = more sensitive. We map this to a pixel-area threshold.
    sensitivity = float(config.get("sensitivity", 0.4))
    min_area = max(200, int((1.0 - sensitivity) * (CAMERA_W * CAMERA_H * 0.02)))

    print(
        f"[sentry] capture starting: device={CAMERA_DEVICE} {CAMERA_W}x{CAMERA_H}@{CAMERA_FPS}fps "
        f"pre={pre_sec}s post={post_sec}s sensitivity={sensitivity} (min_area_px={min_area})",
        flush=True,
    )

    cap = cv.VideoCapture(CAMERA_DEVICE)
    if not cap.isOpened():
        # Try numeric index as a fallback (some boards expose only /dev/video0
        # but cv2 prefers the int form).
        cap = cv.VideoCapture(0)
    if not cap.isOpened():
        raise RuntimeError(f"could not open camera {CAMERA_DEVICE}")
    cap.set(cv.CAP_PROP_FRAME_WIDTH, CAMERA_W)
    cap.set(cv.CAP_PROP_FRAME_HEIGHT, CAMERA_H)
    cap.set(cv.CAP_PROP_FPS, CAMERA_FPS)

    bg = cv.createBackgroundSubtractorMOG2(history=200, varThreshold=25, detectShadows=False)
    frame_interval = 1.0 / max(1, CAMERA_FPS)
    pre_roll: Deque[Tuple[float, "cv.Mat"]] = collections.deque(maxlen=pre_sec * CAMERA_FPS)

    bucket = _bucket()

    try:
        while not _stop_flag.is_set():
            loop_start = time.monotonic()
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.1)
                continue

            ts = time.monotonic()
            pre_roll.append((ts, frame.copy()))

            mask = bg.apply(frame)
            mask = cv.medianBlur(mask, 5)
            _, thresh = cv.threshold(mask, 200, 255, cv.THRESH_BINARY)
            contours, _ = cv.findContours(thresh, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
            motion_area = sum(cv.contourArea(c) for c in contours)

            now = time.monotonic()
            if motion_area >= min_area and (now - _last_motion_at) >= SENTRY_COOLDOWN_S:
                with _state_lock:
                    _last_motion_at = now
                try:
                    _record_and_upload(cap, cv, bucket, db, unit_id, list(pre_roll), post_sec)
                except Exception as e:
                    print(f"[sentry] record/upload failed: {e}", flush=True)
                # After event, refresh background so the new scene isn't motion.
                bg = cv.createBackgroundSubtractorMOG2(
                    history=200, varThreshold=25, detectShadows=False,
                )

            # Pace the loop to CAMERA_FPS.
            elapsed = time.monotonic() - loop_start
            slack = frame_interval - elapsed
            if slack > 0:
                time.sleep(slack)
    finally:
        cap.release()
        print("[sentry] capture stopped", flush=True)


def _record_and_upload(
    cap,
    cv,
    bucket,
    db: firestore.Client,
    unit_id: str,
    pre_roll_frames: List[Tuple[float, "cv.Mat"]],
    post_sec: int,
) -> None:
    epoch_ms = int(time.time() * 1000)
    storage_dir = f"units/{unit_id}/clips"
    clip_path = f"{storage_dir}/{epoch_ms}.mp4"
    thumb_path = f"{storage_dir}/{epoch_ms}.jpg"

    print(f"[sentry] motion → recording clip {clip_path}", flush=True)

    # Encode to a temp .mp4 with mp4v codec — works headlessly on Linux.
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name
    fourcc = cv.VideoWriter_fourcc(*"mp4v")
    writer = cv.VideoWriter(tmp_path, fourcc, CAMERA_FPS, (CAMERA_W, CAMERA_H))

    # Pre-roll first.
    for _ts, f in pre_roll_frames:
        writer.write(f)

    # Use the first pre-roll frame as the thumbnail.
    thumb_frame = pre_roll_frames[0][1] if pre_roll_frames else None

    # Post-roll — read live frames.
    post_frames = post_sec * CAMERA_FPS
    for _ in range(post_frames):
        if _stop_flag.is_set():
            break
        ok, frame = cap.read()
        if not ok:
            break
        writer.write(frame)
        time.sleep(1.0 / max(1, CAMERA_FPS))

    writer.release()

    # Upload clip.
    blob = bucket.blob(clip_path)
    blob.upload_from_filename(tmp_path, content_type="video/mp4")
    os.unlink(tmp_path)

    # Upload thumbnail.
    if thumb_frame is not None:
        ok, jpeg = cv.imencode(".jpg", thumb_frame, [cv.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            tblob = bucket.blob(thumb_path)
            tblob.upload_from_string(jpeg.tobytes(), content_type="image/jpeg")

    # Mirror live state.
    db.document(f"units/{unit_id}/current/sentry").set({
        "armed": True,
        "lastMotionAt": firestore.SERVER_TIMESTAMP,
        "lastClipPath": clip_path,
    }, merge=True)

    # Append event — the Cloud Function picks this up and sends a push.
    db.collection(f"units/{unit_id}/events").add({
        "kind": "motion",
        "at": firestore.SERVER_TIMESTAMP,
        "source": "pi",
        "clipPath": clip_path,
        "thumbnailPath": thumb_path,
    })

    print(f"[sentry] clip uploaded ({clip_path})", flush=True)


# ─── armed-state controller ─────────────────────────────────────────────────

def start_sentry(db: firestore.Client, unit_id: str) -> threading.Thread:
    """Subscribe to config/sentry. When enabled flips true, start the capture
    thread; when false, stop it. Idempotent — repeat calls are no-ops."""
    global _capture_thread, _stop_flag

    config_ref = db.document(f"units/{unit_id}/config/sentry")
    state_lock_local = threading.Lock()

    def on_config(snap_iter, _changes, _read_time):
        global _capture_thread, _stop_flag
        if not snap_iter:
            return
        snap = snap_iter[0] if isinstance(snap_iter, list) else snap_iter
        data = snap.to_dict() if hasattr(snap, "to_dict") else {}
        enabled = bool(data.get("enabled", False))
        with state_lock_local:
            running = _capture_thread is not None and _capture_thread.is_alive()
            if enabled and not running:
                _stop_flag = threading.Event()
                config_data = data or {}

                def runner():
                    try:
                        _capture_loop(db, unit_id, config_data)
                    except Exception as e:
                        print(f"[sentry] capture crashed: {e}", flush=True)

                _capture_thread = threading.Thread(
                    target=runner, daemon=True, name="sentry-capture",
                )
                _capture_thread.start()
                db.document(f"units/{unit_id}/current/sentry").set({
                    "armed": True,
                }, merge=True)
            elif not enabled and running:
                _stop_flag.set()
                # Don't join — daemon thread will exit on its own; we want
                # the listener callback to return quickly.
                db.document(f"units/{unit_id}/current/sentry").set({
                    "armed": False,
                }, merge=True)

    # Wrap the callback for Firestore's expected signature (col_snapshot, changes, read_time).
    def snapshot_handler(col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name in ("ADDED", "MODIFIED"):
                on_config([change.document], None, read_time)

    # Use a document watch — config/sentry is a single doc.
    config_ref.on_snapshot(
        lambda doc_snapshot, changes, read_time: on_config(doc_snapshot, changes, read_time)
    )

    # Return a sentinel thread so the caller sees the same interface as
    # the other start_* services.
    placeholder = threading.Thread(target=lambda: None, daemon=True, name="sentry-controller")
    placeholder.start()
    return placeholder
