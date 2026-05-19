"""Bench harness: send servo.set commands and observe ACK + mirror.
Run on the Pi:  SITEPULSE_SA=~/sitepulse/service-account.json python3 _bench_servo_cmd.py
"""
import os, sys, time
import firebase_admin
from firebase_admin import credentials, firestore

SA = os.path.expanduser(os.environ.get("SITEPULSE_SA", "~/sitepulse/service-account.json"))
UNIT = os.environ.get("SITEPULSE_UNIT_ID", "UNIT-001")

cred = credentials.Certificate(SA)
firebase_admin.initialize_app(cred)
db = firestore.client()


def send(kind, payload, wait_s=5.0):
    ref = db.collection(f"units/{UNIT}/commands").document()
    ref.set({
        "kind": kind,
        "payload": payload,
        "status": "pending",
        "createdAt": firestore.SERVER_TIMESTAMP,
        "createdBy": "bench-test",
    })
    print(f"  sent {kind} {payload}  (cmd {ref.id[:8]})")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        snap = ref.get().to_dict() or {}
        st = snap.get("status")
        if st in ("ack", "failed"):
            err = snap.get("error")
            tag = "OK" if st == "ack" else "FAIL"
            print(f"    -> {tag} err={err}")
            return st == "ack"
        time.sleep(0.1)
    print("    -> TIMEOUT")
    return False


def show_mirror():
    snap = db.document(f"units/{UNIT}/current/servos").get().to_dict() or {}
    keys = sorted(snap.keys())
    for k in keys:
        print(f"    {k}: {snap[k]}")


print("--- throttle 0.0 -> 1.0 ---")
send("servo.set", {"servo": "throttle", "position": 1.0})
time.sleep(1.5)
print("--- throttle 1.0 -> 0.0 ---")
send("servo.set", {"servo": "throttle", "position": 0.0})
time.sleep(1.5)
print("--- choke 0.0 -> 0.5 ---")
send("servo.set", {"servo": "choke", "position": 0.5})
time.sleep(1.0)
print("--- preset start_cold ---")
send("servo.preset", {"name": "start_cold"})
time.sleep(2.0)
print("--- preset idle ---")
send("servo.preset", {"name": "idle"})
time.sleep(1.5)
print("--- current/servos ---")
show_mirror()
