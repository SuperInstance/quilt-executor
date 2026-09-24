"""Pins: LayaSampledJudge wired into the Evaluator judge seam (SPEC v2 §6)."""
from __future__ import annotations

import asyncio
import sys
import unittest

sys.path.insert(0, "/tmp/quilt-executor")

from executor.ledger import Ledger, TaskRequest, TaskResult  # noqa: E402
import executor.evaluator as ev_mod  # noqa: E402
from executor.plugins.laya_gate import LayaSampledJudge  # noqa: E402


def _run(ledger: Ledger, judge, task_id: str, output: str = "done"):
    req = TaskRequest(task_id=task_id, task_type="code", prompt="p")
    res = TaskResult(provider="kimi", output=output, latency_ms=2.0, cost_usd=0.0)
    res.metadata.clear()
    score = asyncio.run(ev_mod.Evaluator().evaluate(req, res, judge=judge))
    return score, res


class TestSampledJudge(unittest.TestCase):
    def test_sampling_is_deterministic_and_approx_rate(self):
        j = LayaSampledJudge(sample_rate=0.10)
        ids = [f"t-{i}" for i in range(1000)]
        flags = [j.sampled(t) for t in ids]
        self.assertEqual(flags, [j.sampled(t) for t in ids])  # stable
        frac = sum(flags) / len(flags)
        self.assertTrue(0.05 <= frac <= 0.16, f"sampled fraction {frac} off 10%")

    def test_zero_rate_never_escalates(self):
        j = LayaSampledJudge(sample_rate=0.0)
        for i in range(200):
            self.assertFalse(j.sampled(f"x-{i}"))
        self.assertEqual(j._cut, 0)

    def test_full_rate_always_escalates(self):
        j = LayaSampledJudge(sample_rate=1.0)
        for i in range(200):
            self.assertTrue(j.sampled(f"x-{i}"))

    def test_heuristics_path_marks_metadata(self):
        led = Ledger(name="pin")
        ev_mod.bind_ledger(led)
        j = LayaSampledJudge(sample_rate=0.0)  # nobody sampled
        _, res = _run(led, j, "any-id")
        self.assertEqual(res.metadata.get("laya_path"), "heuristics_only")
        self.assertFalse(res.metadata.get("laya_sampled"))

    def test_escalated_path_consults_laya_and_books_absences(self):
        led = Ledger(name="pin")
        ev_mod.bind_ledger(led)
        j = LayaSampledJudge(sample_rate=1.0)
        # long output breaches the 512-token budget -> truncation absences fire
        score, res = _run(led, j, "esc-1", output="word " * 700)
        self.assertTrue(res.metadata.get("laya_sampled"))
        laya_md = res.metadata.get("laya")
        self.assertIsNotNone(laya_md)
        self.assertGreaterEqual(len(laya_md.get("absences", [])), 1)
        types = {a["absence_type"] for a in laya_md["absences"]}
        self.assertGreaterEqual(len(types), 1)
        for t in types:
            self.assertIn(t,
                          {"noul_ambiguity", "insufficient_context", "ood",
                           "router_failure", "truncation", "timeout"})
        self.assertTrue(led.verify()[0])

    def test_bandit_neutrality_through_seam(self):
        """Even at 100% escalation, no decider arm update happens (no decider bound)."""
        led = Ledger(name="pin")
        ev_mod.bind_ledger(led)
        j = LayaSampledJudge(sample_rate=1.0)
        _run(led, j, "n-1", output="fine")
        # no exception + chain intact == neutrality by construction holds
        self.assertTrue(led.verify()[0])


if __name__ == "__main__":
    unittest.main()
