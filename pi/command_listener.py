"""
SitePulse command listener.

Single Firestore `on_snapshot` listener on
    units/{UNIT_ID}/commands  where status == 'pending'
that dispatches each new command to a domain handler and writes the ACK
(or failure) back to the same document.

Handlers are imported lazily so this module stays importable on a bench
Pi that doesn't yet have the relay or camera hardware wired — missing
hardware shows up as a runtime error on a specific command kind, not a
broken process.

Setup on the Pi (alongside firebase_publisher.py):
    pip3 install --break-system-packages firebase-admin
    SITEPULSE_SA=~/sitepulse/service-account.json python3 command_listener.py

Env vars:
    SITEPULSE_UNIT_ID    default UNIT-001
    SITEPULSE_SA         path to service account JSON
    SITEPULSE_DEBUG      "1" for verbose dispatch logging
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Dict

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter


UNIT_ID         = os.environ.get("SITEPULSE_UNIT_ID", "UNIT-001")
SERVICE_ACCOUNT = os.path.expanduser(
    os.environ.get("SITEPULSE_SA", "~/sitepulse/service-account.json")
)
DEBUG           = os.environ.get("SITEPULSE_DEBUG") == "1"


# ─── handler registry ───────────────────────────────────────────────────────
#
# Each handler takes (db, unit_id, payload) and either returns silently on
# success or raises. The dispatcher converts exceptions into status='failed'
# with the exception message in the `error` field.

Handler = Callable[[firestore.Client, str, Dict[str, Any]], None]


def _not_implemented(kind: str) -> Handler:
    def stub(_db: firestore.Client, _unit: str, _payload: Dict[str, Any]) -> None:
        raise NotImplementedError(
            f"command kind '{kind}' has no handler wired yet"
        )
    return stub


def _lazy(module_path: str, attr: str, kind: str) -> Handler:
    """Defer import so hardware modules don't break process startup."""
    def wrapped(db: firestore.Client, unit: str, payload: Dict[str, Any]) -> None:
        try:
            mod = __import__(module_path, fromlist=[attr])
        except Exception as e:
            raise RuntimeError(
                f"handler '{kind}' tried to import {module_path}.{attr}: {e}"
            ) from e
        fn = getattr(mod, attr, None)
        if fn is None:
            raise RuntimeError(
                f"handler '{kind}': {module_path} has no attribute '{attr}'"
            )
        fn(db, unit, payload)
    return wrapped


# Wire each CommandKind to a callable. Stubs raise NotImplementedError
# (logged + ACK'd with status='failed') until the hardware module lands.
HANDLERS: Dict[str, Handler] = {
    # Phase B
    "light.set":         _lazy("relays", "handle_light_set",      "light.set"),
    "relay.set":         _lazy("relays", "handle_relay_set",      "relay.set"),
    # Phase C
    "engine.override":   _lazy("scheduler", "handle_engine_override", "engine.override"),
    "charge.update":     _lazy("scheduler", "handle_charge_update",   "charge.update"),
    "schedule.update":   _lazy("scheduler", "handle_charge_update",   "schedule.update"),
    # Phase D
    "sentry.arm":        _lazy("sentry",   "handle_sentry_arm",       "sentry.arm"),
    "sentry.disarm":     _lazy("sentry",   "handle_sentry_disarm",    "sentry.disarm"),
    "sentry.update":     _lazy("sentry",   "handle_sentry_update",    "sentry.update"),
    "camera.startStream": _lazy("streamer", "handle_camera_start",    "camera.startStream"),
    "camera.stopStream":  _lazy("streamer", "handle_camera_stop",     "camera.stopStream"),
    # Phase E
    "servo.set":         _lazy("servos",  "handle_servo_set",         "servo.set"),
    "servo.preset":      _lazy("servos",  "handle_servo_preset",      "servo.preset"),
    "servo.update":      _lazy("servos",  "handle_servo_update",      "servo.update"),
    # Legacy / phase 1 — already-shipped outlet relays
    "outlet.toggle":     _not_implemented("outlet.toggle"),
    "theft.arm":         _not_implemented("theft.arm"),
    "theft.disarm":      _not_implemented("theft.disarm"),
}


# ─── dispatch loop ──────────────────────────────────────────────────────────

# Firestore's on_snapshot delivers the *entire* result set on every change,
# so a freshly-ACK'd command stays in the snapshot until the next change
# updates it out of the `status == pending` filter. We track which command
# IDs we've already handed off to a worker thread to avoid re-running them
# in the few-ms window before the ACK write lands.
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def _log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{stamp}] [cmd] {msg}", flush=True)


def _ack(doc_ref: firestore.DocumentReference, ok: bool, error: str | None = None) -> None:
    update: Dict[str, Any] = {
        "status": "ack" if ok else "failed",
        "ackAt":  firestore.SERVER_TIMESTAMP,
    }
    if error is not None:
        update["error"] = error
    try:
        doc_ref.update(update)
    except Exception as e:
        _log(f"!!! ack write failed for {doc_ref.id}: {e}")


def _run_command(db: firestore.Client, doc_ref: firestore.DocumentReference, data: Dict[str, Any]) -> None:
    cmd_id = doc_ref.id
    kind = data.get("kind", "")
    payload = data.get("payload", {}) or {}
    handler = HANDLERS.get(kind)

    if handler is None:
        _log(f"  {cmd_id} kind='{kind}' — no handler registered")
        _ack(doc_ref, ok=False, error=f"unknown command kind: {kind}")
        return

    if DEBUG:
        _log(f"  {cmd_id} kind='{kind}' payload={payload}")

    try:
        handler(db, UNIT_ID, payload)
        _ack(doc_ref, ok=True)
        _log(f"  {cmd_id} kind='{kind}' OK")
    except NotImplementedError as e:
        _log(f"  {cmd_id} kind='{kind}' stub: {e}")
        _ack(doc_ref, ok=False, error=str(e))
    except Exception as e:
        tb = traceback.format_exc(limit=3)
        _log(f"  {cmd_id} kind='{kind}' FAILED: {e}\n{tb}")
        _ack(doc_ref, ok=False, error=str(e))
    finally:
        with _inflight_lock:
            _inflight.discard(cmd_id)


def _on_snapshot(db: firestore.Client):
    def handler(col_snapshot, changes, _read_time) -> None:
        for change in changes:
            if change.type.name not in ("ADDED", "MODIFIED"):
                continue
            doc = change.document
            data = doc.to_dict() or {}
            if data.get("status") != "pending":
                continue
            cmd_id = doc.id
            with _inflight_lock:
                if cmd_id in _inflight:
                    continue
                _inflight.add(cmd_id)
            # Run each command on its own thread so a slow handler (e.g.
            # ffmpeg startup) doesn't block the listener. Firestore SDK's
            # callbacks fire on a single background thread.
            threading.Thread(
                target=_run_command,
                args=(db, doc.reference, data),
                daemon=True,
                name=f"cmd-{cmd_id[:8]}",
            ).start()
    return handler


def init_firestore() -> firestore.Client:
    if not os.path.exists(SERVICE_ACCOUNT):
        print(
            f"[fatal] service account not found at {SERVICE_ACCOUNT}",
            file=sys.stderr,
        )
        sys.exit(1)
    if not firebase_admin._apps:
        cred = credentials.Certificate(SERVICE_ACCOUNT)
        firebase_admin.initialize_app(cred)
    return firestore.client()


def _start_background_services(db: firestore.Client) -> None:
    """
    Boot the long-running background services that ride alongside the
    command listener. Each is wrapped so a missing hardware dep (e.g.
    RPi.GPIO on a Mac dev box) doesn't crash the listener — the failure
    is logged once and the rest of the dispatch loop stays alive.
    """
    services = [
        ("relays.start_override_watcher", lambda: __import__(
            "relays", fromlist=["start_override_watcher"]
        ).start_override_watcher(db, UNIT_ID)),
        ("scheduler.start_scheduler", lambda: __import__(
            "scheduler", fromlist=["start_scheduler"]
        ).start_scheduler(db, UNIT_ID)),
        ("sentry.start_sentry", lambda: __import__(
            "sentry", fromlist=["start_sentry"]
        ).start_sentry(db, UNIT_ID)),
        ("streamer.start_streamer", lambda: __import__(
            "streamer", fromlist=["start_streamer"]
        ).start_streamer(db, UNIT_ID)),
        ("servos.start_servos", lambda: __import__(
            "servos", fromlist=["start_servos"]
        ).start_servos(db, UNIT_ID)),
    ]
    for name, starter in services:
        try:
            starter()
            _log(f"started background service: {name}")
        except Exception as e:
            _log(f"!!! background service '{name}' failed to start: {e}")


def main() -> None:
    db = init_firestore()
    _start_background_services(db)

    query = (
        db.collection(f"units/{UNIT_ID}/commands")
          .where(filter=FieldFilter("status", "==", "pending"))
    )

    _log(f"listening on units/{UNIT_ID}/commands (status==pending)")
    watch = query.on_snapshot(_on_snapshot(db))

    # Graceful shutdown so systemd can stop us cleanly.
    stop = threading.Event()

    def _shutdown(_signum, _frame):
        _log("shutdown signal received")
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        try:
            watch.unsubscribe()
        except Exception:
            pass
        # Detach servos so PWM stops and linkages spring-return. If start
        # failed (no hardware), detach_all is a safe no-op.
        try:
            from servos import detach_all as _servos_detach
            _servos_detach()
        except Exception as e:
            _log(f"shutdown: servos.detach_all failed: {e}")
        _log("listener stopped")


if __name__ == "__main__":
    main()
