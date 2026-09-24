"""Sealed pins for LayaEvaluator (SPEC v2). Run from the build root:
    python3 -m unittest discover -s tests -q
Zero network: every test injects MockAdapter and a fresh in-memory Ledger."""
import asyncio
import importlib.util
import os
import sys
import unittest
from pathlib import Path

BUILD_ROOT = Path(__file__).resolve().parents[1]
REPO = Path(os.environ.get("QUILT_EXECUTOR_REPO", "/tmp/quilt-executor"))
sys.path.insert(0, str(REPO))

from executor.ledger import Ledger, TaskRequest, TaskResult  # noqa: E402
import executor.evaluator as ev  # noqa: E402
from executor.decider import ThompsonDecider  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "executor.laya_evaluator", BUILD_ROOT / "executor" / "laya_evaluator.py")
le = importlib.util.module_from_spec(_spec)
sys.modules["executor.laya_evaluator"] = le  # dataclass postponed-annotation resolution
_spec.loader.exec_module(le)


def make_eval(adapter=None, clock=None, gate_table=None, season_length=100.0):
    gt = gate_table or le.CalibrationTable(season_length_ticks=season_length)
    adapter = adapter or le.MockAdapter()
    evaluator = le.LayaEvaluator(gate_table=gt, clock=clock or le.ScriptedClock([0.0, 1.0]),
                                 adapter=adapter)
    return evaluator, gt, adapter


def run(evaluator, output="some real output text here", context=None, task_type="code"):
    req = TaskRequest(task_id="t1", prompt="do a thing", task_type=task_type,
                      context=context or {})
    res = TaskResult(output, "kimi", 800.0, 0.0002)
    return req, res, asyncio.run(evaluator(req, res))


class TestLayaEvaluator(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger("laya-pins")
        ev.bind_ledger(self.ledger)

    # 1. per-axis fallback firing: low-confidence completeness -> heuristics;
    #    confident honesty/conciseness -> laya; reference -> correctness.
    def test_per_axis_fallback_firing(self):
        evaluator, _, _ = make_eval()
        req, res, served = run(evaluator, context={"expected": "real output"})
        meta = res.metadata["laya"]
        backends = {r["axis"]: r["backend"] for r in meta["receipts"] if "axis" in r}
        self.assertEqual(backends["correctness"], "reference")
        self.assertEqual(backends["completeness"], "heuristics")  # mock conf 0.41 < 0.55
        self.assertEqual(backends["honesty"], "laya")
        self.assertEqual(set(served), {"honesty", "conciseness"})

    # 2. absence row schema + verify(): forced truncation books typed rows
    #    that pass the §3 schema pin, and the chain verifies.
    def test_absence_row_schema_and_verify(self):
        evaluator, _, _ = make_eval()
        _, res, served = run(evaluator, output="x" * 5000)
        self.assertEqual(served, {})
        self.assertEqual([r.kind for r in self.ledger.rows], ["REFUSED"] * 4)
        self.assertTrue(self.ledger.verify()[0])
        for row in self.ledger.rows:
            ok, problems = le.verify_absence_row(row.body["absence"])
            self.assertTrue(ok, problems)
            self.assertEqual(row.body["absence"]["absence_type"], "truncation")
        self.assertEqual(len(res.metadata["laya"]["absences"]), 4)

    # 3. truncation precondition: coverage math + unknown term recorded.
    def test_truncation_precondition(self):
        evaluator, _, _ = make_eval()
        _, res, _ = run(evaluator, output="y" * 6000)
        trunc = res.metadata["laya"]["truncation"]
        self.assertTrue(trunc["forced"])
        self.assertGreater(trunc["ratio"], le.TRUNC_RATIO_TAU)
        self.assertLess(trunc["coverage"], 1.0)  # unseen tail is provable
        self.assertEqual(set(res.metadata["laya"]["unknown_axes"]),
                         {"correctness", "completeness", "honesty", "conciseness"})

    # 4. gate_table cold stratum fails closed: no refit + non-english -> ESCALATE.
    def test_gate_table_cold_stratum_fail_closed(self):
        gt = le.CalibrationTable()
        self.assertEqual(gt.lookup("code", "multilingual", "le512", 0), le.ESCALATE)
        gt.fit("code", "multilingual", "le512", 0, 0.61)
        self.assertEqual(gt.lookup("code", "multilingual", "le512", 0), 0.61)
        self.assertEqual(gt.lookup("code", "multilingual", "le512", 1), le.ESCALATE)  # season expiry
        self.assertEqual(gt.lookup("code", "english", "le512", 0), 0.55)
        self.assertEqual(gt.lookup("code", "english", "le512", 0, truncated=True), 0.60)
        self.assertEqual(gt.lookup("code", "english", "le512", 0, routing_ambiguous=True),
                         le.ESCALATE)

    # 5. cold stratum escalates through the evaluator (ood, never gated).
    def test_cold_stratum_escalates_ood(self):
        evaluator, _, adapter = make_eval()
        adapter.forced_route = {"checkpoint_id": "multilingual", "router_score": 0.9,
                                "alternatives": [], "detection": {"script": "cyrillic"},
                                "ambiguous": False, "served_by": "multilingual@mock-laya"}
        _, res, served = run(evaluator)
        self.assertEqual(served, {})
        types = {a["absence_type"] for a in res.metadata["laya"]["absences"]}
        self.assertEqual(types, {"ood"})
        self.assertTrue(all(a["escalation"]["escalated_to"] == "judge"
                            for a in res.metadata["laya"]["absences"]))

    # 6. bandit neutrality: absences never move alpha/beta; ground truth does.
    def test_bandit_neutrality_on_absences(self):
        class FakeProvider:
            def __init__(self, name):
                self.name = name
        providers = [FakeProvider("kimi")]
        decider = ThompsonDecider(providers, seed=1)
        req = TaskRequest(task_id="t", prompt="p", task_type="code")
        before = decider.posterior_table()
        evaluator, _, _ = make_eval()
        evaluator._decider = decider
        run(evaluator, output="z" * 5000)  # all-absence path
        self.assertEqual(decider.posterior_table(), before)
        le.record_ground_truth(decider, req, "kimi", 0.9)
        self.assertNotEqual(decider.posterior_table(), before)

    # 7. dual-clock separation: scripted ticks vs walls land in booked rows.
    def test_dual_clock_separation(self):
        clock = le.ScriptedClock(ticks=[1000.0, 1033.0], walls=[5000.0, 5041.0])
        evaluator, _, _ = make_eval(clock=clock)
        _, res, _ = run(evaluator, output="w" * 5000)
        self.assertEqual(res.metadata["laya"]["cost"], {"tick_ms": 33.0, "wall_ms": 41.0})
        for row in self.ledger.rows:
            self.assertEqual(row.body["absence"]["cost_tick_ms"], 33.0)
            self.assertEqual(row.body["absence"]["cost_wall_ms"], 41.0)

    # 8. lease evidence accumulation: bars live, weight decays, season expires.
    def test_lease_evidence_accumulation(self):
        book = le.LeaseBook(half_life_ticks=100.0, min_pairs=3, p99_delta_max=0.25)
        for i in range(3):
            book.record_pair("code", 0, 0.8, 0.82, tick=10.0 + i)
        st = book.status("code", 0, tick=12.0)
        self.assertTrue(st["granted"], st)
        self.assertAlmostEqual(st["weight"], 1.0)
        self.assertAlmostEqual(book.weight("code", 0, tick=112.0), 0.5)  # one half-life
        book.record_pair("code", 0, 0.1, 0.95, tick=20.0)  # p99 breach
        self.assertFalse(book.status("code", 0, tick=20.0)["granted"])
        self.assertEqual(book.weight("code", 0, tick=20.0), 0.0)
        self.assertFalse(book.status("code", 1, tick=20.0)["granted"])  # season boundary

    # 9. string-only state enforcement (the dict-state Router bug).
    def test_string_only_state_enforcement(self):
        evaluator, _, _ = make_eval()
        original = evaluator._state
        evaluator._state = lambda req, res, ctx: {"prompt": "dict state"}  # type: ignore
        req = TaskRequest(task_id="t", prompt="p", task_type="code")
        res = TaskResult("out", "kimi", 1.0, 0.0)
        with self.assertRaises(TypeError):
            asyncio.run(evaluator(req, res))
        evaluator._state = original

    # 10. empty TaskResult degenerate path: typed absences, no crash, no serve.
    def test_empty_result_degenerate_path(self):
        evaluator, _, _ = make_eval()
        req = TaskRequest(task_id="t", prompt="p", task_type="code")
        res = TaskResult("", "kimi", 1.0, 0.0, error="provider returned empty body")
        served = asyncio.run(evaluator(req, res))
        self.assertEqual(served, {})
        meta = res.metadata["laya"]
        self.assertEqual(set(meta["unknown_axes"]),
                         {"correctness", "completeness", "honesty", "conciseness"})
        self.assertTrue(meta["absences"])
        self.assertTrue(all(a["provider"] == "laya" for a in meta["absences"]))

    # 11. routing-ambiguous rows escalate, never gate.
    def test_routing_ambiguous_escalates(self):
        evaluator, _, adapter = make_eval()
        adapter.forced_route = {"checkpoint_id": "english", "router_score": 0.5,
                                "alternatives": [], "detection": {"is_english": False},
                                "ambiguous": True, "served_by": "english@mock-laya"}
        _, res, served = run(evaluator)
        self.assertEqual(served, {})
        types = {a["absence_type"] for a in res.metadata["laya"]["absences"]}
        self.assertEqual(types, {"router_failure"})
        receipts = res.metadata["laya"]["receipts"]
        self.assertFalse(any(r.get("why") == "confidence_below_gate" for r in receipts))

    # 12. refusal_worthy >= 0.7 skips prior seeding with a receipt.
    def test_prior_skipped_on_refusal_worthy(self):
        class FakeProvider:
            name = "kimi"
        decider = ThompsonDecider([FakeProvider()], seed=1)
        req = TaskRequest(task_id="t", prompt="p", task_type="code")
        receipt = le.seed_priors(decider, req, "kimi", {"honesty": 1.0}, 0.85)
        self.assertEqual(receipt["kind"], "prior_skipped")
        self.assertEqual(receipt["noul"], 0.85)
        self.assertEqual(decider.posterior_table(), {})  # nothing seeded
        seeded = le.seed_priors(decider, req, "kimi", {"honesty": 1.0}, 0.1)
        self.assertEqual(seeded["kind"], "prior_seeded")
        arm = decider._arm("code", "kimi")
        self.assertAlmostEqual(arm.alpha, 1.0 + 1.0 * le.PRIOR_STRENGTH)

    # 13. noul_ambiguity absence fires inside the ambiguous band [0.3, 0.7).
    def test_noul_ambiguity_absence(self):
        evaluator, _, adapter = make_eval()
        answers = {"correctness": {"type": "score", "score": 2, "confidence": 0.9},
                   "completeness": {"type": "score", "score": 2, "confidence": 0.9},
                   "honesty": {"type": "score", "score": 2, "confidence": 0.9},
                   "conciseness": {"type": "score", "score": 2, "confidence": 0.9},
                   "refusal_worthy": {"type": "noul", "noul": 0.5, "confidence": 0.6}}
        adapter.forced_answers = answers
        _, res, _ = run(evaluator)
        types = [a["absence_type"] for a in res.metadata["laya"]["absences"]]
        self.assertEqual(types, ["noul_ambiguity"])
        rw = res.metadata["laya"]["refusal_worthy"]
        self.assertFalse(rw["applied"])

    # 14. predict raise -> timeout absences with checkpoint_unreachable guard.
    def test_timeout_absence_on_predict_raise(self):
        evaluator, _, adapter = make_eval()
        adapter.forced_error = le.CheckpointUnreachable("weights missing (no GPU)")
        _, res, served = run(evaluator)
        self.assertEqual(served, {})
        for a in res.metadata["laya"]["absences"]:
            self.assertEqual(a["absence_type"], "timeout")
            self.assertEqual(a["trigger"]["guard"], "checkpoint_unreachable")

    # 15. end-to-end through the real Evaluator seam: judge used, chain holds.
    def test_judge_seam_end_to_end(self):
        evaluator, _, _ = make_eval()
        req = TaskRequest(task_id="t1", prompt="do a thing", task_type="code",
                          context={"expected": "real output"})
        res = TaskResult("some real output text here", "kimi", 800.0, 0.0002)
        score = asyncio.run(ev.Evaluator().evaluate(req, res, judge=evaluator))
        self.assertGreater(score.overall, 0.0)
        self.assertTrue(res.metadata["judge"].startswith("used:"))
        kinds = [r.kind for r in self.ledger.rows]
        self.assertEqual(kinds, ["EFFECT"])  # no absences on the happy path
        self.assertTrue(self.ledger.verify()[0])
        self.assertEqual(len(res.metadata["laya"]["calibration"]), 2)  # honesty+conciseness


if __name__ == "__main__":
    unittest.main()
