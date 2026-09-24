"""Pins for gravity-guided search — intentionality from seeds.

FAIL-first where possible: each pin proves the field pulls search away from
the uniform lottery, reproducibly, with visible refusals on degenerate input.
"""
from __future__ import annotations

import math
import sys
import unittest

sys.path.insert(0, "/tmp/quilt-executor")

from executor.gravity import GravityField, MothCell, JepaCell, JevCell, mulberry32
from executor.ledger import Ledger


def dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


class TestGravityField(unittest.TestCase):
    def test_same_seed_same_stream(self):
        f1 = GravityField(dim=3, seed=42)
        f2 = GravityField(dim=3, seed=42)
        f1.light("lamp", (0.8, 0.8, 0.8), 5.0)
        f2.light("lamp", (0.8, 0.8, 0.8), 5.0)
        s1 = [f1.propose() for _ in range(50)]
        s2 = [f2.propose() for _ in range(50)]
        self.assertEqual(s1, s2)  # intentionality is reproducible, not hoped

    def test_different_seed_different_stream(self):
        f1 = GravityField(dim=2, seed=1)
        f2 = GravityField(dim=2, seed=2)
        self.assertNotEqual([f1.propose() for _ in range(8)],
                            [f2.propose() for _ in range(8)])

    def test_lamps_pull_toward_light_vs_control(self):
        # landscape: one bright lamp at (0.9, 0.9)
        lamp = (0.9, 0.9)
        field = GravityField(dim=2, seed=7, lamp_pull=0.7, jepa_pull=0.0)
        field.light("peak", lamp, 10.0)
        grav = [field.propose() for _ in range(400)]
        # control: identical stream shape but pure uniform
        ctrl_rng = mulberry32(7)
        ctrl = [tuple(-1 + 2 * (ctrl_rng() / 0xFFFFFFFF) for _ in range(2))
                for _ in range(400)]
        med = lambda pts: sorted(dist(p, lamp) for p in pts)[len(pts) // 2]
        self.assertLess(med(grav), med(ctrl) * 0.55)  # gravity wins decisively

    def test_jepa_direction_pulls_along_history(self):
        field = GravityField(dim=2, seed=11, lamp_pull=0.0, jepa_pull=1.0, step=0.2)
        # train: moving +x from origin always improved, -x always hurt
        for _ in range(20):
            off = (0.3, 0.0)
            field.jepa.train((0.0, 0.0), off, 1.0)
            field.jepa.train((0.0, 0.0), (-0.3, 0.0), -1.0)
        dx = [field.propose()[0] for _ in range(200)]
        self.assertGreater(sum(dx) / len(dx), 0.05)  # net pull along +x

    def test_jev_gate_quiet_then_surprised(self):
        jev = JevCell(k=1.5, warm=3)
        for v in (10.0, 10.1, 9.9, 10.0):
            jev.surprise(v)
        quiet_before = jev.quiet_pulls
        surprised, _ = jev.surprise(10.05)   # inside the belief: quiet
        self.assertFalse(surprised)
        self.assertGreater(jev.quiet_pulls, quiet_before)
        surprised, dev = jev.surprise(50.0)  # breaks the belief: react
        self.assertTrue(surprised)

    def test_degenerate_field_books_refused_not_crash(self):
        led = Ledger(name="g")
        field = GravityField(dim=2, seed=3)
        ok = field.observe((0.0, 0.0), 1.0, ledger=led)
        self.assertFalse(ok)
        self.assertEqual(led.rows[-1].kind, "REFUSED")
        self.assertIn("degenerate", led.rows[-1].body["reason"])
        ok_chain, problems = led.verify()
        self.assertTrue(ok_chain, problems)

    def test_observe_books_receipts_and_trains(self):
        led = Ledger(name="g")
        field = GravityField(dim=2, seed=5)
        field.light("a", (0.0, 0.0), 1.0)
        field.observe((0.1, 0.1), 2.0, ledger=led)   # surprise (warm) -> EFFECT
        kinds = [r.kind for r in led.rows]
        self.assertIn("EFFECT", kinds)
        self.assertTrue(field.jepa.pairs)            # direction layer learned
        ok, problems = led.verify()
        self.assertTrue(ok, problems)
        profile = field.profile(seed=5)
        self.assertEqual(profile["seed"], 5)
        self.assertEqual(len(profile["field_digest"]), 16)


if __name__ == "__main__":
    unittest.main()
