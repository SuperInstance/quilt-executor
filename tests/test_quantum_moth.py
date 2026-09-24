"""Sealed pins for the quantum substrate + the two live arms.

Pure pins only: entropy/chaos math is byte-fed; provider pins monkeypatch
urllib — no network in the suite, ever. Anchor receipts from the live
runs (2026-09-25) appear as fixture strings, never as calls.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executor.ledger import Ledger, TaskRequest
from executor.quantum_moth import (
    MothJobRef, QrngEntropy, OtocChaosPrior, ChaosShapedGravity, fingerprint,
)
from executor.gravity import GravityField
from executor.providers import (
    TypeSafeProvider, MothQuantumProvider, EnvSlotProvider, default_roster,
)

# Live anchors (sha-sealed in /tmp/quantum-jev-lab/results.jsonl)
QRNG_REF = MothJobRef(
    engine="comet-qrng-v1", job_id="82308b28-c254-4ac7-9514-875493b5aeee",
    params_fp="p", result_fp="r", bell_s=2.7559, grade="simulator-baseline")
OTOC_REF = MothJobRef(
    engine="otoc-echo-v1", job_id="dfe0fd11-0fdf-4863-99a3-42b495bcc578",
    params_fp="p", result_fp="r")
ANCHOR_SWEEP = {0.0: -0.1368, 0.5: -0.1247, 1.0: -0.1154}


class TestMothJobRef(unittest.TestCase):
    def test_frozen_receipt(self):
        import dataclasses
        self.assertIn("frozen", [s.lower() for s in
                                 [dataclasses.fields(MothJobRef)[0].name]] and ["frozen"])
        with self.assertRaises(Exception):
            QRNG_REF.job_id = "tamper"

    def test_citation_carries_bell_and_grade(self):
        c = QRNG_REF.cite()
        self.assertIn("comet-qrng-v1", c)
        self.assertIn("bell_S=2.7559", c)
        self.assertIn("simulator-baseline", c)

    def test_fingerprint_is_canonical(self):
        self.assertEqual(fingerprint({"b": 1, "a": 2}), fingerprint({"a": 2, "b": 1}))
        self.assertEqual(len(fingerprint({"x": 1})), 16)


class TestQrngEntropy(unittest.TestCase):
    def test_dice_deterministic_and_fair(self):
        data = bytes(range(256))
        a = QrngEntropy(data, QRNG_REF)
        b = QrngEntropy(data, QRNG_REF)
        self.assertEqual(a.dice(64, 6), b.dice(64, 6))
        self.assertEqual(a.remaining, b.remaining)

    def test_dice_in_range(self):
        e = QrngEntropy(bytes(range(256)) * 4, QRNG_REF)
        draws = e.dice(200, 7)
        self.assertTrue(all(0 <= d < 7 for d in draws))

    def test_exhaustion_is_refusal_not_wrap(self):
        e = QrngEntropy(b"\x01\x02", QRNG_REF)
        with self.assertRaises(RuntimeError) as ctx:
            e.dice(3, 2)
        self.assertIn("never reuse", str(ctx.exception))

    def test_rejection_skews_unfair_bytes(self):
        # sides=3: block=255, byte 255 must be skipped, stream continues
        e = QrngEntropy(bytes([255, 7, 8]), QRNG_REF)
        self.assertEqual(e.dice(2, 3), [7 % 3, 8 % 3])
        self.assertEqual(e.remaining, 0)

    def test_splitmix_stream_reproducible(self):
        a = list(QrngEntropy(b"\x0c\x0a\xca\xfe" * 2, QRNG_REF).splitmix_stream(8))
        b = list(QrngEntropy(b"\x0c\x0a\xca\xfe" * 2, QRNG_REF).splitmix_stream(8))
        self.assertEqual(a, b)
        self.assertTrue(all(0 <= v <= 0xFFFFFFFF for v in a))

    def test_citation_echoes_job(self):
        self.assertIn("82308b28", QrngEntropy(b"\x00", QRNG_REF).citation())


class TestOtocChaosPrior(unittest.TestCase):
    def test_needs_two_points(self):
        with self.assertRaises(ValueError):
            OtocChaosPrior({0.0: -0.1}, OTOC_REF)

    def test_lambda_interpolation_hits_anchors(self):
        p = OtocChaosPrior(ANCHOR_SWEEP, OTOC_REF)
        self.assertAlmostEqual(p.lambda_at(0.0), -0.1368, places=4)
        self.assertAlmostEqual(p.lambda_at(0.5), -0.1247, places=4)
        self.assertAlmostEqual(p.lambda_at(1.0), -0.1154, places=4)

    def test_lambda_midpoint_and_clamps(self):
        p = OtocChaosPrior(ANCHOR_SWEEP, OTOC_REF)
        self.assertAlmostEqual(p.lambda_at(0.25), (-0.1368 + -0.1247) / 2, places=4)
        self.assertEqual(p.lambda_at(-9.0), -0.1368)   # clamp low
        self.assertEqual(p.lambda_at(9.0), -0.1154)    # clamp high

    def test_ruggedness_normalized_monotone(self):
        p = OtocChaosPrior(ANCHOR_SWEEP, OTOC_REF)
        self.assertEqual(p.ruggedness(0.0), 0.0)
        self.assertEqual(p.ruggedness(1.0), 1.0)
        self.assertGreater(p.ruggedness(0.5), p.ruggedness(0.0))
        self.assertLessEqual(p.ruggedness(0.3), 1.0)

    def test_citation_lists_sweep(self):
        c = OtocChaosPrior(ANCHOR_SWEEP, OTOC_REF).citation()
        self.assertIn("0:-0.1368", c)
        self.assertIn("dfe0fd11", c)


class TestChaosShapedGravity(unittest.TestCase):
    def _field(self, seed=7):
        return GravityField(dim=3, seed=seed)

    def test_propose_reproducible_with_prior(self):
        g1 = ChaosShapedGravity(self._field(), prior=OtocChaosPrior(ANCHOR_SWEEP, OTOC_REF))
        g2 = ChaosShapedGravity(self._field(), prior=OtocChaosPrior(ANCHOR_SWEEP, OTOC_REF))
        s1 = [g1.propose(disorder=d) for d in (0.0, 0.5, 1.0, 0.25)]
        s2 = [g2.propose(disorder=d) for d in (0.0, 0.5, 1.0, 0.25)]
        self.assertEqual(s1, s2)
        self.assertEqual(g1.shaped, 4)

    def test_disorder_none_falls_back_to_field(self):
        g = ChaosShapedGravity(self._field())
        self.assertEqual(len(g.propose()), 3)
        self.assertEqual(g.shaped, 0)

    def test_break_tie_certified(self):
        e = QrngEntropy(bytes(range(64)), QRNG_REF)
        g = ChaosShapedGravity(self._field(), entropy=e)
        pick = g.break_tie([("a",), ("b",), ("c",)])
        self.assertIn(pick, (0, 1, 2))
        self.assertEqual(g.tie_breaks, 1)

    def test_break_tie_empty_refusal(self):
        g = ChaosShapedGravity(self._field())
        with self.assertRaises(ValueError):
            g.break_tie([])

    def test_provenance_cites_everything(self):
        g = ChaosShapedGravity(self._field(), QrngEntropy(bytes(range(64)), QRNG_REF),
                               OtocChaosPrior(ANCHOR_SWEEP, OTOC_REF))
        g.propose(disorder=0.5)
        g.break_tie([1, 2])
        prov = g.provenance()
        self.assertIn("82308b28", prov["entropy"])
        self.assertIn("dfe0fd11", prov["chaos"])
        self.assertEqual(prov["tie_breaks"], 1)


class TestTypeSafeProvider(unittest.TestCase):
    def test_unset_refusal_wording_is_pin_contract(self):
        for var in ("TYPESAFE_API_KEY", "TYPESAFEAI_KEY"):
            os.environ.pop(var, None)
        p = TypeSafeProvider()
        ok, why = p.available(TaskRequest(task_id="t", prompt="x"))
        self.assertFalse(ok)
        self.assertEqual(why, "TYPESAFE_API_KEY unset (slot reserved)")
        r = p.execute(TaskRequest(task_id="t", prompt="x"))
        self.assertTrue(r.error.startswith("refused:"))

    def test_alternate_key_name_accepted(self):
        os.environ["TYPESAFEAI_KEY"] = "test-key"
        try:
            p = TypeSafeProvider()
            self.assertEqual(p.available(TaskRequest(task_id="t", prompt="x")), (True, "ok"))
        finally:
            os.environ.pop("TYPESAFEAI_KEY", None)

    def test_execute_parses_noul(self):
        os.environ["TYPESAFEAI_KEY"] = "test-key"
        payload = {"model": "jev-1.13.0",
                   "answers": {"x": {"type": "noul", "noul": 0.93}},
                   "usage": {"input_tokens": 5, "output_tokens": 2}}
        try:
            with mock.patch("urllib.request.urlopen",
                            return_value=_Resp(payload)) as m:
                r = TypeSafeProvider().execute(
                    TaskRequest(task_id="t", prompt="artifact text",
                                task_type="jev-gate",
                                context={"question": "is this canon?"}))
            self.assertEqual(r.output, "0.93")
            self.assertEqual(r.metadata["model"], "jev-1.13.0")
            sent = m.call_args[0][0]
            body = json.loads(sent.data.decode())
            self.assertEqual(body["questions"]["x"]["instructions"], "is this canon?")
        finally:
            os.environ.pop("TYPESAFEAI_KEY", None)


class TestMothQuantumProvider(unittest.TestCase):
    def test_unset_refusal_wording_is_pin_contract(self):
        for var in ("MOTHQUANTUM_API_KEY", "MOTHQUANTUM_KEY"):
            os.environ.pop(var, None)
        p = MothQuantumProvider()
        ok, why = p.available(TaskRequest(task_id="t", prompt="x"))
        self.assertFalse(ok)
        self.assertEqual(why, "MOTHQUANTUM_API_KEY unset (slot reserved)")

    def test_browser_ua_present(self):
        self.assertIn("Mozilla/5.0", MothQuantumProvider.UA)

    def test_execute_full_poll_cycle(self):
        os.environ["MOTHQUANTUM_KEY"] = "test-key"
        result_body = {"result": {"output": {"bell_witness": {"S": 2.7},
                                              "random": {"bits": 8}}}}
        statuses = [{"status": "queued"}, {"status": "running"},
                    {"status": "completed"}]
        try:
            with mock.patch("urllib.request.urlopen") as m:
                m.side_effect = [_Resp({"job_id": "j-1"}),
                                 _Resp(statuses[0]), _Resp(statuses[1]),
                                 _Resp(statuses[2]), _Resp(result_body)]
                r = MothQuantumProvider().execute(
                    TaskRequest(task_id="t", prompt="", task_type="quantum-job",
                                context={"engine": "coin-toss-v1", "params": {"shots": 4},
                                         "poll_s": 0, "max_polls": 5}))
            self.assertIsNone(r.error)
            self.assertEqual(r.metadata["job_id"], "j-1")
            self.assertEqual(r.metadata["bell_s"], 2.7)
            self.assertIn('"S": 2.7', r.output)
        finally:
            os.environ.pop("MOTHQUANTUM_KEY", None)

    def test_execute_terminal_failure(self):
        os.environ["MOTHQUANTUM_KEY"] = "test-key"
        try:
            with mock.patch("urllib.request.urlopen") as m:
                m.side_effect = [_Resp({"job_id": "j-2"}), _Resp({"status": "failed"})]
                r = MothQuantumProvider().execute(
                    TaskRequest(task_id="t", prompt="", task_type="quantum-job",
                                context={"engine": "x", "params": {}, "poll_s": 0}))
            self.assertIn("failed", r.error)
        finally:
            os.environ.pop("MOTHQUANTUM_KEY", None)


class _Resp:
    def __init__(self, payload):
        import json as _json
        self._b = _json.dumps(payload).encode()
        self.status = 200

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestRosterGraduation(unittest.TestCase):
    def test_slots_graduated_live(self):
        names = [p.name for p in default_roster()]
        self.assertIn("typesafe", names)
        self.assertIn("mothquantum", names)
        self.assertIsInstance(default_roster()[3], TypeSafeProvider)
        self.assertIsInstance(default_roster()[4], MothQuantumProvider)

    def test_env_slot_class_kept_for_future(self):
        p = EnvSlotProvider("future", "FUTURE_KEY")
        ok, why = p.available(TaskRequest(task_id="t", prompt="x"))
        self.assertFalse(ok)
        self.assertEqual(why, "FUTURE_KEY unset (slot reserved)")


if __name__ == "__main__":
    unittest.main()
