"""Offline tests for the RPM-confirmed catch in engine.start.

Run: python3 pi/test_engine_rpm_confirm.py
No CAN bus, relays or Firestore needed — the hardware calls are stubbed.
"""
import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402


class _Msg:
    def __init__(self, arb, erpm):
        self.is_extended_id = True
        self.arbitration_id = arb
        self.data = struct.pack(">i", erpm) + b"\x00" * 4


class _FakeSender:
    """Replays a list of eRPM readings as STATUS_1 frames, 0.1 s apart."""
    readings: list = []

    def __init__(self, iface=None, unit_id=0):
        self.unit_id = unit_id
        self._bus = self
        self._i = 0

    def open(self):
        pass

    def close(self):
        pass

    def recv(self, timeout=None):
        _clock[0] += 0.1
        if self._i >= len(self.readings):
            return None
        erpm = self.readings[self._i]
        self._i += 1
        return _Msg((engine.CMD_STATUS_1 << 8) | (self.unit_id & 0xFF), erpm)


_clock = [0.0]


def _fake_monotonic():
    return _clock[0]


class WaitForRpmConfirm(unittest.TestCase):
    def setUp(self):
        _clock[0] = 0.0
        self.p = [
            mock.patch.object(engine, "VescSender", _FakeSender),
            mock.patch.object(engine.time, "monotonic", _fake_monotonic),
        ]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_confirms_after_hold(self):
        # Sign is ignored: this VESC reports running as negative on some units.
        _FakeSender.readings = [-10_000] * 20 + [-60_000] * 20
        ok, peak, _ = engine._wait_for_rpm_confirm(56_000, 25, 0.5)
        self.assertTrue(ok)
        self.assertEqual(peak, 60_000)

    def test_sputter_is_not_confirmed(self):
        # 2.5-3.5k mech RPM at 14 pole pairs — sputtering, never reaches 4k.
        _FakeSender.readings = [35_000, 49_000, 30_000, 42_000] * 100
        ok, peak, _ = engine._wait_for_rpm_confirm(56_000, 25, 0.5)
        self.assertFalse(ok)
        self.assertEqual(peak, 49_000)

    def test_brief_spike_does_not_confirm(self):
        _FakeSender.readings = ([20_000] * 5 + [60_000] * 2) * 60
        ok, _, _ = engine._wait_for_rpm_confirm(56_000, 25, 0.5)
        self.assertFalse(ok)


class StartSequence(unittest.TestCase):
    def _run(self, start_cfg, confirm_result):
        states = []
        spark = []
        cfg = dict(engine.DEFAULT_START_CONFIG, **start_cfg)
        with mock.patch.object(engine, "_load_start_config", return_value=cfg), \
             mock.patch.object(engine, "_load_crank_config",
                               return_value=dict(engine.DEFAULT_CRANK_CONFIG)), \
             mock.patch.object(engine, "_do_crank", return_value=engine._CrankResult(
                 "running", {"durationSec": 1.2, "catchSignal": "low_current_hold"})), \
             mock.patch.object(engine, "_wait_for_rpm_confirm",
                               return_value=confirm_result) as wait, \
             mock.patch.object(engine, "_publish_state",
                               side_effect=lambda db, u, s, extra=None: states.append((s, extra or {}))), \
             mock.patch.object(engine, "_set_spark_safely",
                               side_effect=lambda db, u, ch, mode: spark.append(mode)), \
             mock.patch.object(engine.time, "sleep"):
            engine.handle_engine_start(None, "UNIT-TEST", {})
        return states, spark, wait

    def test_disabled_keeps_old_behaviour(self):
        states, spark, wait = self._run({}, (False, None, 0))
        wait.assert_not_called()
        self.assertEqual(states[-1][0], "running")
        self.assertEqual(spark, ["on"])

    def test_confirmed_start_runs(self):
        states, spark, wait = self._run({"rpmConfirmEnabled": True}, (True, 70_000, 8.0))
        # 4000 mech × 14 pole pairs; window less the 2 s post-catch settle.
        self.assertEqual(wait.call_args[0][:2], (56_000, 23.0))
        self.assertEqual(states[-1][0], "running")
        self.assertEqual(states[-1][1]["confirmedRpm"], 5000)
        self.assertNotIn("running", [s for s, _ in states[:-1]])
        self.assertEqual(spark, ["on"])

    def test_sputter_fails_no_catch_and_kills_spark(self):
        states, spark, _ = self._run({"rpmConfirmEnabled": True}, (False, 42_000, 23.0))
        state, meta = states[-1]
        self.assertEqual(state, "failed_no_catch")
        self.assertEqual(meta["reason"], "rpm_not_confirmed")
        self.assertEqual(meta["peakRpm"], 3000)
        self.assertNotIn("running", [s for s, _ in states])
        self.assertEqual(spark, ["on", "off"])


if __name__ == "__main__":
    unittest.main()
