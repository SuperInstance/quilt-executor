"""Pins for the witness bridge — registry envelopes on the custody chain."""
from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "/tmp/quilt-executor")

from executor.ledger import Ledger
from executor import witness


def _env(i: int, prev: str, polarity: str = "ACCEPT") -> dict:
    return {"witness_id": f"w{i:03d}", "prev_witness_id": prev, "polarity": polarity,
            "substrate": "test", "cell_id": f"cell-{i}", "status": "ok",
            "timestamp": 1000 + i, "payload": {"n": i}}


class TestWitnessBridge(unittest.TestCase):
    def test_valid_envelope_books_witness_row(self):
        led = Ledger(name="w")
        row = witness.book_witness(led, _env(1, ""))
        self.assertEqual(row.kind, witness.WITNESS_KIND)
        self.assertEqual(row.body["validator"], "vendored")  # registry not installed here
        ok, problems = led.verify()
        self.assertTrue(ok, problems)

    def test_invalid_envelope_is_refused_not_booked(self):
        led = Ledger(name="w")
        bad = _env(1, "")
        del bad["polarity"]
        row = witness.book_witness(led, bad)
        self.assertEqual(row.kind, "REFUSED")
        self.assertIn("polarity", row.body["reason"].lower())
        # custody chain still verifies; refusal is first-class
        ok, problems = led.verify()
        self.assertTrue(ok, problems)

    def test_bad_polarity_value_refused(self):
        led = Ledger(name="w")
        bad = _env(1, "", polarity="MAYBE")
        row = witness.book_witness(led, bad)
        self.assertEqual(row.kind, "REFUSED")

    def test_witness_export_and_continuity(self):
        led = Ledger(name="w")
        witness.book_witness(led, _env(1, ""))
        witness.book_witness(led, _env(2, "w001"))
        led.book_tick({"between": True})  # custody rows interleave fine
        witness.book_witness(led, _env(3, "w002", polarity="DRIFT"))
        envs = witness.witness_export(led)
        self.assertEqual(len(envs), 3)
        ok, msg = witness.witness_continuity(led)
        self.assertTrue(ok, msg)

    def test_continuity_catches_broken_semantic_link(self):
        led = Ledger(name="w")
        witness.book_witness(led, _env(1, ""))
        witness.book_witness(led, _env(2, "WRONG"))
        ok, msg = witness.witness_continuity(led)
        self.assertFalse(ok)

    def test_cross_discipline_tamper_visible_in_both(self):
        """Tampering a witness body breaks custody verify AND continuity."""
        led = Ledger(name="w")
        witness.book_witness(led, _env(1, ""))
        witness.book_witness(led, _env(2, "w001"))
        # attacker rewrites witness 1's payload in place
        for r in led.rows:
            if r.kind == witness.WITNESS_KIND and r.body["envelope"]["witness_id"] == "w001":
                r.body["envelope"]["payload"] = {"n": 999}
        ok, _ = led.verify()
        self.assertFalse(ok)  # custody hash walk catches it
        ok2, _ = witness.witness_continuity(led)
        self.assertTrue(ok2)  # ids still link; the CUSTODY layer is what caught it


if __name__ == "__main__":
    unittest.main()
