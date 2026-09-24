"""Sealed pins for the quilt-optimization bridge. Run: python3 -m unittest discover -s tests -q"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
# quilt-optimization is an OPTIONAL sibling dependency; make it importable
# when a source checkout exists (QUILT_OPT_SRC override for other boxes).
_SRC = os.environ.get("QUILT_OPT_SRC", "/tmp/sib-opt/src")
if os.path.isdir(_SRC):
    sys.path.insert(0, _SRC)

try:
    import quilt_optimization  # noqa: F401
    HAVE_SUBSTRATE = True
except ImportError:
    HAVE_SUBSTRATE = False

from executor.ledger import Ledger, TaskRequest
from router.fleet_router import DispatchTask
import router.quilt_opt_bridge as bridge


def mk_task(tid, utility, cost=None, lat=None, risk=None, deps=()):
    return DispatchTask(
        request=TaskRequest(task_id=tid, prompt=f"do {tid}", task_type="demo"),
        utility=utility,
        est_cost=cost or {p: 0.001 for p in utility},
        est_latency_ms=lat or {p: 1000.0 for p in utility},
        context_risk=risk or {p: 0.0 for p in utility},
        depends_on=list(deps),
    )


@unittest.skipUnless(HAVE_SUBSTRATE, "quilt-optimization source checkout absent")
class TestFormulation(unittest.TestCase):
    def test_binary_vars_one_per_task_and_caps(self):
        tasks = [mk_task("a", {"kimi": 0.9, "claude": 0.8}),
                 mk_task("b", {"kimi": 0.7})]
        prob = bridge.build_lp_problem(tasks, {"kimi": 2, "claude": 1})
        # binary: every var INTEGER in [0,1]
        for v in prob.variables:
            self.assertEqual(v.vtype, "INTEGER")
            self.assertEqual((v.lb, v.ub), (0.0, 1.0))
        self.assertEqual(len(prob.variables), 3)  # a×2 providers + b×1
        # one ==1 constraint per task; one cap constraint per provider
        eqs = [c for c in prob.constraints if c.sense == "=="]
        self.assertEqual({c.name for c in eqs},
                         {"one_provider:a", "one_provider:b"})
        caps = {c.name: c.rhs for c in prob.constraints if c.name.startswith("cap:")}
        self.assertEqual(caps, {"cap:claude": 1.0, "cap:kimi": 2.0})
        self.assertEqual(prob.objective.sense, "MAXIMIZE")

    def test_objective_coeffs_priced(self):
        t = mk_task("a", {"kimi": 0.9}, cost={"kimi": 0.01},
                    lat={"kimi": 500.0}, risk={"kimi": 0.2})
        prob = bridge.build_lp_problem([t], {"kimi": 1}, lam=1.0, mu=0.001, nu=0.5)
        coeff = prob.objective.terms[0].coefficient
        self.assertAlmostEqual(coeff, 0.9 - 0.01 - 0.5 - 0.1)

    def test_unmet_dependency_is_typed_error(self):
        t = mk_task("b", {"kimi": 0.7}, deps=["a"])  # 'a' not in batch
        with self.assertRaises(ValueError):
            bridge.build_lp_problem([t], {"kimi": 1})


@unittest.skipUnless(HAVE_SUBSTRATE, "quilt-optimization source checkout absent")
class TestSolveAndWitness(unittest.TestCase):
    def test_assignment_respects_caps_and_witness_chains(self):
        tasks = [mk_task("a", {"kimi": 0.9, "claude": 0.8}),
                 mk_task("b", {"kimi": 0.85, "claude": 0.4}),
                 mk_task("c", {"kimi": 0.5, "claude": 0.95})]
        L = Ledger()
        asg = bridge.solve_via_substrate(tasks, {"kimi": 2, "claude": 2}, L)
        # every task dispatched exactly once
        self.assertEqual(sorted(asg), ["a", "b", "c"])
        # provider caps respected
        self.assertLessEqual(sum(1 for p in asg.values() if p == "kimi"), 2)
        self.assertLessEqual(sum(1 for p in asg.values() if p == "claude"), 2)
        # c is claude's flagship (0.95 − 0 vs kimi 0.5): LP should agree
        self.assertEqual(asg["c"], "claude")
        # witness landed in our ledger
        tick = L.rows[-1].body["summary"]
        self.assertEqual(tick["solver"], "quilt-optimization-substrate")
        self.assertEqual(tick["polarity"], "ACCEPT")
        self.assertTrue(tick["witness_id"])
        self.assertEqual(L.verify()[0], True)
        # two solves chain: same substrate cell, prev links forward
        bridge._default_substrate = None  # deterministic chain start
        L2 = Ledger()
        bridge.solve_via_substrate(tasks[:1], {"kimi": 1, "claude": 1}, L2)
        first = L2.rows[-1].body["summary"]["witness_id"]
        bridge.solve_via_substrate(tasks[1:2], {"kimi": 1, "claude": 1}, L2)
        self.assertEqual(L2.rows[-1].body["summary"]["prev_witness_id"], first)


class TestRefusalPath(unittest.TestCase):
    def test_absent_substrate_books_refused_not_silence(self):
        tasks = [mk_task("a", {"kimi": 0.9})]
        L = Ledger()
        with mock.patch.object(bridge, "_import_substrate",
                               side_effect=ImportError("No module named 'quilt_optimization'")):
            asg = bridge.solve_via_substrate(tasks, {"kimi": 1}, L)
        self.assertIsNone(asg)
        refused = [r for r in L.rows if r.kind == "REFUSED"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].body["provider"], "quilt-optimization")
        self.assertIn("substrate absent", refused[0].body["reason"])
        self.assertEqual(L.verify()[0], True)


if __name__ == "__main__":
    unittest.main()
