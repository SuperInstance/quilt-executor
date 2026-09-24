"""Lease -> gate wiring pins (BUILD_NOTES #7). Run from the build root:
    python3 -m unittest discover -s tests -q
Zero network, stdlib only. A wired-in LeaseBook's LIVE weight authorizes
gating: grant keeps the table gate, decay tightens it toward 1.0, and a
missing/expired grant escalates as typed ``ood`` — bandit-neutral throughout.
"""
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
sys.modules["executor.laya_evaluator"] = le
_spec.loader.exec_module(le)

OUT = "some real output text here, long enough to score"


class _P:
    """Duck-typed provider (ThompsonDecider only reads the seam)."""
    name = "kimi"

    def estimate_cost(self, request):
        return 0.0

    def estimate_latency_ms(self, request):
        return 0.0

    def available(self, request):
        return True


def granted_book(task_type="code", season_id=0, tick=0.0, half_life=10_000.0):
    """A LeaseBook freshly granted at ``tick``: 200 near-zero-delta pairs,
    zero refusals — every bar green at the grant tick."""
    book = le.LeaseBook(half_life_ticks=half_life)
    for _ in range(200):
        book.record_pair(task_type, season_id, 0.5, 0.49, tick=tick)
    return book


def make_eval(lease_book, tick0=0.0, season_length=100_000.0, adapter=None):
    gt = le.CalibrationTable(season_length_ticks=season_length)
    evaluator = le.LayaEvaluator(
        gate_table=gt, adapter=adapter or le.MockAdapter(),
        clock=le.ScriptedClock([tick0, tick0 + 1.0]), lease_book=lease_book)
    return evaluator


def run(evaluator, task_type="code", context=None):
    # reference context routes correctness to the reference backend (outranks
    # laya per SPEC §5 #2), matching the sealed harness in test_laya_evaluator.
    req = TaskRequest(task_id="t1", prompt="do a thing", task_type=task_type,
                      context=context if context is not None else {"expected": "real output"})
    res = TaskResult(OUT, "kimi", 800.0, 0.0002)
    return req, res, asyncio.run(evaluator(req, res))


class TestLeaseGateWiring(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger("lease-gate-pins")
        ev.bind_ledger(self.ledger)

    # 1. no LeaseBook wired in -> behavior identical to the standalone table
    #    (regression pin: wiring is strictly opt-in).
    def test_no_lease_book_unchanged(self):
        evaluator = le.LayaEvaluator(
            gate_table=le.CalibrationTable(season_length_ticks=100_000.0),
            adapter=le.MockAdapter(),
            clock=le.ScriptedClock([0.0, 1.0]))
        req, res, served = run(evaluator)
        self.assertEqual(set(served), {"honesty", "conciseness"})
        self.assertIsNone(res.metadata["laya"]["lease"])

    # 2. wired book with NO evidence -> escalate, four typed ``ood`` rows with
    #    the lease guard, schema-pinned, booked on the one ledger.
    def test_no_grant_escalates_as_ood(self):
        evaluator = make_eval(le.LeaseBook(), tick0=0.0)
        req, res, served = run(evaluator)
        meta = res.metadata["laya"]
        self.assertEqual(served, {})
        self.assertEqual(meta["gate"]["value"], le.ESCALATE)
        self.assertEqual(meta["gate"]["lease"], "not_granted")
        self.assertFalse(meta["lease"]["granted"])
        self.assertEqual(len(meta["absences"]), 4)
        for row in meta["absences"]:
            self.assertEqual(row["absence_type"], "ood")
            self.assertEqual(row["trigger"]["guard"], "lease")
            ok, problems = le.verify_absence_row(row)
            self.assertTrue(ok, problems)
        kinds = [r.kind for r in self.ledger.rows]
        self.assertEqual(kinds.count("REFUSED"), 4)

    # 3. fresh grant (weight 1.0) -> the table gate passes through untouched:
    #    exactly the same axes serve as with no book at all.
    def test_fresh_grant_passes_table_gate(self):
        evaluator = make_eval(granted_book(tick=0.0), tick0=0.0)
        req, res, served = run(evaluator)
        meta = res.metadata["laya"]
        self.assertEqual(set(served), {"honesty", "conciseness"})
        self.assertTrue(meta["lease"]["granted"])
        self.assertEqual(meta["lease"]["weight"], 1.0)
        self.assertEqual(meta["gate"]["lease_weighted"], 0.55)
        self.assertEqual(meta["absences"], [])

    # 4. one half-life of decay (weight exactly at the floor boundary is still
    #    allowed; strictly below escalates — pinned at 2 half-lives, w=0.25):
    #    no absence rows, but the tightened gate demotes conciseness.
    def test_decay_tightens_gate(self):
        book = granted_book(tick=0.0)
        evaluator = make_eval(book, tick0=10_000.0)
        req, res, served = run(evaluator)
        meta = res.metadata["laya"]
        self.assertEqual(meta["lease"]["weight"], 0.5)
        self.assertEqual(meta["gate"]["lease_weighted"], 0.775)
        self.assertEqual(set(served), {"honesty"})  # conciseness 0.62 < 0.775
        self.assertEqual(meta["absences"], [])

    # 5. two half-lives (w=0.25 < floor) -> escalate as typed ``ood`` naming
    #    the floor, nothing served.
    def test_expired_lease_escalates(self):
        book = granted_book(tick=0.0)
        evaluator = make_eval(book, tick0=20_000.0)
        req, res, served = run(evaluator)
        meta = res.metadata["laya"]
        self.assertEqual(served, {})
        self.assertEqual(meta["gate"]["value"], le.ESCALATE)
        self.assertIn("0.25", meta["gate"]["lease"])
        self.assertEqual(len(meta["absences"]), 4)
        self.assertTrue(all(r["absence_type"] == "ood" and
                            r["trigger"]["guard"] == "lease"
                            for r in meta["absences"]))

    # 6. hard constraint: wiring never touches bandit arms, granted or not.
    def test_bandit_neutral(self):
        decider = ThompsonDecider([_P()], seed=0)
        before = {(c, p): (a.alpha, a.beta)
                  for c, arms in decider.arms.items() for p, a in arms.items()}
        for book, tick in ((le.LeaseBook(), 0.0), (granted_book(tick=0.0), 0.0),
                           (granted_book(tick=0.0), 20_000.0)):
            evaluator = make_eval(book, tick0=tick)
            evaluator._decider = decider
            run(evaluator)
        after = {(c, p): (a.alpha, a.beta)
                 for c, arms in decider.arms.items() for p, a in arms.items()}
        self.assertEqual(before, after)  # empty before AND after: never fed

    # 7. routing ambiguity still outranks the lease: an ambiguous route with
    #    a wired book escalates as router_failure, not ood.
    def test_ambiguity_outranks_lease(self):
        adapter = le.MockAdapter()
        adapter.forced_route = {"checkpoint_id": "multilingual", "ambiguous": True,
                                "router_score": 0.5, "alternatives": [],
                                "detection": {"language_undecided": True}}
        evaluator = make_eval(le.LeaseBook(), tick0=0.0, adapter=adapter)
        req, res, served = run(evaluator)
        meta = res.metadata["laya"]
        self.assertEqual({r["absence_type"] for r in meta["absences"]},
                         {"router_failure"})
        self.assertEqual(served, {})


if __name__ == "__main__":
    unittest.main()
