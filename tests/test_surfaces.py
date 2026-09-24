"""Portability-ladder pins (Rungs 2–5): tools, web, cells, embedded manifest."""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request

sys.path.insert(0, "/tmp/quilt-executor")
sys.path.insert(0, "/tmp/quilt-executor/ports/embedded")

from executor.ledger import Ledger, TaskRequest, TaskResult, canonical_hash  # noqa: E402
from executor.tools import ExecutorTools  # noqa: E402
import executor.evaluator as ev_mod2  # noqa: E402
from executor import cells  # noqa: E402
import ledger_micro  # noqa: E402

CAFE_PIN = "0x24a555471370b18d"


class TestTools(unittest.TestCase):
    def test_five_verbs_receipt(self):
        t = ExecutorTools(ledger=Ledger(name="pin"))
        self.assertTrue(t.tick("pin")["ticked"])
        d = t.dispatch("t1", ["kimi", "claude"])
        self.assertIn(d["chosen"], ("kimi", "claude"))
        s = t.score("say ok", "ok done", provider="pin")
        self.assertGreaterEqual(s["overall"], 0.0)
        self.assertTrue(t.verify()["verify"])
        self.assertGreaterEqual(t.rows()["count"], 2)  # tick + scored-effect rows

    def test_dispatch_no_providers_refused(self):
        led = Ledger(name="pin")
        t = ExecutorTools(ledger=led)
        d = t.dispatch("t-empty", [])
        self.assertIsNone(d["chosen"])
        self.assertTrue(any(r.kind == "REFUSED" for r in led.rows))


class TestCells(unittest.TestCase):
    def test_sheet_roundtrip(self):
        led = Ledger(name="pin")
        led.book_tick({"n": 1})
        led.book_refused(TaskRequest(task_id="x", prompt=""), "pin", "why-not")
        grid = cells.to_sheet(led)
        self.assertEqual(grid["A1"]["opcode"], "TICK")
        self.assertEqual(grid["A2"]["opcode"], "REFUSED")
        replay = cells.from_sheet(grid)
        self.assertEqual(len(replay), 2)
        self.assertEqual(replay[1]["op"], "REFUSED")

    def test_effect_row_books_choice_shape(self):
        """The evaluator books EFFECT as the choice shape {task_id, chosen, rivals} —
        receipts of WHO was chosen, with quality numbers living in result.metadata."""
        led = Ledger(name="pin")
        import executor.evaluator as ev_mod
        ev_mod.bind_ledger(led)
        import asyncio
        res = TaskResult(provider="pin", output="o", latency_ms=1.0, cost_usd=0.0)
        asyncio.run(ev_mod.Evaluator().evaluate(
            TaskRequest(task_id="fx", task_type="score", prompt="p"), res))
        grid = cells.to_sheet(led)
        self.assertEqual(grid["A1"]["opcode"], "EFFECT")
        replay = cells.from_sheet(grid)
        self.assertEqual(replay[0]["op"], "EFFECT")

    def test_port_segment_is_copyable_range(self):
        led = Ledger(name="pin")
        for i in range(5):
            led.book_tick({"n": i})
        seg = cells.port_segment(led, 1, 3)  # rows 2..3 -> A1..A2
        self.assertEqual(len(seg), 2)
        self.assertEqual(seg["A1"]["payload_hash"], cells._hash(led.rows[1].body))

    def test_cafe_pin_same_recipe(self):
        self.assertEqual(cells._hash("café Δ 日本語"), CAFE_PIN)


class TestEmbeddedPort(unittest.TestCase):
    def test_micro_conformance(self):
        self.assertEqual(ledger_micro.fnv1a64("café Δ 日本語"), CAFE_PIN)
        m = ledger_micro.Ledger(actor="pin")
        m.bind({"cell": "pin"})
        m.effect({"v": 1})
        ok, n = m.verify()
        self.assertTrue(ok)
        self.assertEqual(n, 2)

    def test_family_hash_recipe_crosses_substrates(self):
        """Same payload, same recipe: full canonical_hash == micro fnv1a64 (sans 0x)."""
        payload = {"sensor": "temp", "value": 21.5, " café": "Δ 日本語"}
        self.assertEqual(canonical_hash(payload), ledger_micro.fnv1a64(payload)[2:])

    def test_manifest_matches_impl(self):
        m = json.load(open("/tmp/quilt-executor/ports/embedded/manifest.json"))
        vec = m["conformance_vectors"]["café Δ 日本語"]
        self.assertEqual(vec, CAFE_PIN)
        self.assertIn("REFUSED", m["opcodes"])
        self.assertEqual(m["chain"]["prime"], "0x100000001b3")


class TestWeb(unittest.TestCase):
    def test_web_endpoints(self):
        from executor import web
        web.LEDGER, web.EVAL = web._fresh_stack()
        srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        def get(path):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
                return json.loads(r.read())

        def post(path, body):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())

        self.assertTrue(get("/health")["ok"])
        post("/tick", {"note": "pin"})
        s = post("/score", {"prompt": "p", "output": "o", "provider": "pin"})
        self.assertIn("overall", s)
        self.assertTrue(get("/ledger/verify")["verify"])
        rows = get("/ledger/rows")
        self.assertGreaterEqual(rows["count"], 3)  # tick + score + self-effect
        srv.shutdown()


if __name__ == "__main__":
    unittest.main()
