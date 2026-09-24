"""Sealed pins for quilt-executor core. Run: python3 -m pytest tests/ -q OR python3 -m unittest discover -s tests -q"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executor.ledger import Ledger, TaskRequest, TaskResult, canonical_hash, fnv1a64, GENESIS
from executor.decider import ThompsonDecider, MarginalGainRouter
from executor.providers import EnvSlotProvider, CliProvider
from router.fleet_router import DispatchTask, solve_cpu, cuopt_lp, dispatch, cuopt_available


class FakeProvider:
    def __init__(self, name, cost=0.001, lat=1000.0, avail=(True, "ok")):
        self.name, self._c, self._l, self._a = name, cost, lat, avail

    def available(self, req):
        return self._a

    def estimate_cost(self, req):
        return self._c

    def estimate_latency_ms(self, req):
        return self._l


class TestLedger(unittest.TestCase):
    def test_canonical_stable(self):
        # canonical JSON attractor: sorted keys, compact, UTF-8
        self.assertEqual(canonical_hash({"b": 1, "a": 2}), canonical_hash({"a": 2, "b": 1}))

    def test_chain_genesis_and_verify(self):
        L = Ledger()
        self.assertEqual(L.verify(), (True, []))
        req = TaskRequest(task_id="t1", prompt="hello", task_type="demo")
        L.book_bind(req)
        self.assertEqual(L.rows[0].prev_hash, GENESIS)
        L.book_effect(req, TaskResult("out", "kimi", 10.0, 0.1), 0.8,
                      [TaskResult("alt", "crush", 20.0, 0.05, error="refused:not on PATH")])
        L.book_refused(req, "typesafe", "TYPESAFE_API_KEY unset (slot reserved)")
        ok, problems = L.verify()
        self.assertTrue(ok, problems)
        self.assertEqual(len(L.refusals()), 1)

    def test_tamper_loud(self):
        L = Ledger()
        L.book_bind(TaskRequest(task_id="t1", prompt="x"))
        L.rows[0].body["task_id"] = "FORGED"
        ok, problems = L.verify()
        self.assertFalse(ok)
        self.assertTrue(any("row_hash" in p for p in problems))

    def test_amputated_refusal_breaks_chain(self):
        # amputation is only detectable when the refusal has a SUCCESSOR row
        # carrying its hash — the candor 'post-REVOKE writes live again' semantic
        L = Ledger()
        req = TaskRequest(task_id="t1", prompt="x")
        L.book_bind(req)
        L.book_refused(req, "mothquantum", "MOTHQUANTUM_API_KEY unset (slot reserved)")
        L.book_effect(req, TaskResult("out", "kimi", 5.0, 0.1), 0.8, [])
        L.rows.pop(1)  # unpersisted refusal amputates: successor's prev_hash orphans
        ok, problems = L.verify()
        self.assertFalse(ok)
        self.assertTrue(any("chain break" in p for p in problems))


class TestDecider(unittest.TestCase):
    def test_thompson_converges_to_winner(self):
        providers = [FakeProvider("good"), FakeProvider("bad")]
        d = ThompsonDecider(providers, seed=42)
        req = TaskRequest(task_id="t", prompt="p", task_type="code")
        for i in range(60):
            p = d.select(req)
            q = 0.9 if p.name == "good" else 0.1
            d.update(req, p.name, q)
        self.assertEqual(d.rank(req)[0][0], "good")

    def test_budget_gate_falls_back(self):
        providers = [FakeProvider("pricey", cost=1.0), FakeProvider("cheap", cost=0.001)]
        d = ThompsonDecider(providers, seed=1)
        req = TaskRequest(task_id="t", prompt="p", max_cost_usd=0.01)
        for _ in range(10):
            d.update(req, "cheap", 0.7)
        self.assertEqual(d.select(req).name, "cheap")

    def test_marginal_gain_youth_discount(self):
        cheap, exp = FakeProvider("cheap"), FakeProvider("exp", cost=0.01)
        d = ThompsonDecider([cheap, exp], seed=2)
        r = MarginalGainRouter(cheap=cheap, expensive=exp, decider=d)
        req = TaskRequest(task_id="t", prompt="x" * 600, task_type="research", quality_floor=0.9)
        gain_young = r.predicted_gain(req)
        for _ in range(12):
            d.update(req, "cheap", 0.9)
            d.update(req, "exp", 0.95)
        gain_mature = r.predicted_gain(req)
        self.assertLess(gain_young, gain_mature + 1e-9)  # youth shrinks the gap claim


class TestProviders(unittest.TestCase):
    def test_env_slot_refusal_visible(self):
        os.environ.pop("TYPESAFE_API_KEY", None)
        p = EnvSlotProvider("typesafe", "TYPESAFE_API_KEY")
        ok, why = p.available(TaskRequest(task_id="t", prompt="p"))
        self.assertFalse(ok)
        self.assertIn("unset", why)
        res = p.execute(TaskRequest(task_id="t", prompt="p"))
        self.assertTrue(res.error.startswith("refused:"))

    def test_cli_absence_refusal(self):
        p = CliProvider("ghostcli", "definitely-not-a-real-binary-xyz")
        res = p.execute(TaskRequest(task_id="t", prompt="p"))
        self.assertIn("refused:", res.error)


class TestRouter(unittest.TestCase):
    def _tasks(self):
        mk = lambda tid, u, deps=(): DispatchTask(
            request=TaskRequest(task_id=tid, prompt=tid),
            utility=u, est_cost={k: 0.001 for k in u},
            est_latency_ms={k: 1000.0 for k in u},
            context_risk={k: 0.1 for k in u}, depends_on=list(deps))
        return [
            mk("review", {"kimi": 0.9, "crush": 0.5}),
            mk("build", {"kimi": 0.6, "crush": 0.8}, deps=("review",)),
            mk("docs", {"kimi": 0.7, "crush": 0.7}),
        ]

    def test_deterministic_seeded(self):
        a = solve_cpu(self._tasks(), {"kimi": 2, "crush": 2}, random.Random(7))
        b = solve_cpu(self._tasks(), {"kimi": 2, "crush": 2}, random.Random(7))
        self.assertEqual(a, b)
        self.assertEqual(set(a), {"review", "build", "docs"})

    def test_cap_respected(self):
        a = solve_cpu(self._tasks(), {"kimi": 1, "crush": 1}, random.Random(0))
        loads = {}
        for p in a.values():
            loads[p] = loads.get(p, 0) + 1
        self.assertLessEqual(loads.get("kimi", 0), 1)
        self.assertLessEqual(loads.get("crush", 0), 1)

    def test_dag_precedence(self):
        # build depends on review; if review is unassignable, build must not be forced
        tasks = self._tasks()
        tasks[0].utility = {}  # review now has no viable provider
        a = solve_cpu(tasks, {"kimi": 2, "crush": 2}, random.Random(0))
        self.assertNotIn("build", a)

    def test_cuopt_gate_and_receipt(self):
        ok, why = cuopt_available()
        self.assertFalse(ok)  # this box has no GPU — pin the honest refusal
        L = Ledger()
        a = dispatch(self._tasks(), {"kimi": 2, "crush": 2}, L, seed=3)
        self.assertEqual(len(a), 3)
        self.assertTrue(L.verify()[0])
        reasons = [r.body["reason"] for r in L.refusals()]
        self.assertTrue(any("cuopt" in r or "CUDA" in r or "torch" in r or "installed" in r
                            for r in reasons), reasons)
        lp = cuopt_lp(self._tasks(), {"kimi": 2, "crush": 2})
        self.assertEqual(lp["constraints"][0]["name"], "one_provider_per_task")


class TestEvaluatorMemory(unittest.TestCase):
    def setUp(self):
        import executor.evaluator as ev
        self.ev = ev
        self.L = Ledger()
        ev.bind_ledger(self.L)

    def _req_res(self, output="some real output text", error=None):
        req = TaskRequest(task_id="e1", prompt="do a thing", task_type="code")
        res = TaskResult(output, "kimi", 800.0, 0.0002, error=error)
        return req, res

    def test_evaluate_books_effect(self):
        import asyncio
        req, res = self._req_res()
        score = asyncio.run(self.ev.Evaluator().evaluate(req, res))
        self.assertGreaterEqual(score.overall, 0.0)
        self.assertEqual([r.kind for r in self.L.rows], ["EFFECT"])
        self.assertEqual(res.metadata.get("ledger"), "booked:core4")
        self.assertTrue(self.L.verify()[0])

    def test_degenerate_output_scored_zero_not_crash(self):
        import asyncio
        req, res = self._req_res(output="")
        score = asyncio.run(self.ev.Evaluator().evaluate(req, res))
        self.assertEqual(score.overall, 0.0)
        self.assertEqual([r.kind for r in self.L.rows], ["EFFECT"])  # refusal-scored, still receipted

    def test_unbound_ledger_visible_error(self):
        import asyncio
        self.ev.bind_ledger(None)
        req, res = self._req_res()
        asyncio.run(self.ev.Evaluator().evaluate(req, res))
        self.assertIn("error:", res.metadata.get("ledger", ""))
        self.ev.bind_ledger(self.L)

    def test_memory_cache_ledger_reflex(self):
        from executor.memory import ExecutionMemory
        req, res = self._req_res()
        mem = ExecutionMemory(max_size=2)
        mem.put(req, res, 0.8)
        hit = mem.get(req)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.provider.endswith(":cached"))
        self.assertEqual(mem.quality_ledger()["kimi"], 0.8)
        self.assertEqual(mem.reflex_table()[("code", "kimi")]["n"], 1.0)

    def test_memory_eviction_fifo(self):
        from executor.memory import ExecutionMemory
        mem = ExecutionMemory(max_size=1)
        r1, res = self._req_res()
        mem.put(r1, res, 0.8)
        r2 = TaskRequest(task_id="e2", prompt="other prompt", task_type="code")
        mem.put(r2, res, 0.7)
        self.assertIsNone(mem.get(r1))
        self.assertGreater(mem.evictions, 0)


if __name__ == "__main__":
    unittest.main()
