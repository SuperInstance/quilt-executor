"""Pins for QCell — quantum gravity, receipted."""
from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "/tmp/quilt-executor")

from executor import qcell
from executor.gravity import GravityField, mulberry32
from executor.ledger import Ledger

try:
    import qiskit  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False


@unittest.skipUnless(HAVE, "qiskit not installed")
class TestQCell(unittest.TestCase):
    def test_qrng_reproducible_from_seed(self):
        a = qcell.qrng_bits(64, seed=9)
        b = qcell.qrng_bits(64, seed=9)
        self.assertEqual(a, b)
        self.assertNotEqual(qcell.qrng_bits(64, seed=9),
                            qcell.qrng_bits(64, seed=10))

    def test_qrng_looks_uniform(self):
        bits = qcell.qrng_bits(2048, seed=42)
        ones = sum(bits) / len(bits)
        self.assertLess(abs(ones - 0.5), 0.05)  # 2048 draws, generous band

    def test_kernel_self_is_one_and_symmetric(self):
        x, y = (0.1, 0.4, 0.7), (0.6, 0.2, 0.9)
        self.assertAlmostEqual(qcell.kernel(x, x), 1.0, places=6)
        self.assertAlmostEqual(qcell.kernel(x, y), qcell.kernel(y, x), places=9)

    def test_kernel_distinguishes_far_candidates(self):
        x = (0.1, 0.1, 0.1)
        near = qcell.kernel(x, (0.15, 0.12, 0.1))
        far = qcell.kernel(x, (0.9, 0.9, 0.9))
        self.assertGreater(near, far)

    def test_qcell_books_circuit_receipts(self):
        led = Ledger(name="q")
        cell = qcell.QCell(backend="simulator")
        u = cell.uniforms(4, seed=5, ledger=led)
        self.assertEqual(len(u), 4)
        f = cell.similarity((0.1, 0.2), (0.3, 0.4), ledger=led)
        self.assertGreaterEqual(f, 0.0)
        kinds = [r.kind for r in led.rows]
        self.assertEqual(kinds.count("EFFECT"), 2)
        self.assertTrue(all(r.body.get("substrate") == "qcell" for r in led.rows))
        self.assertTrue(all("backend" in r.body for r in led.rows))
        ok, problems = led.verify()
        self.assertTrue(ok, problems)

    def test_absence_books_refused_not_fake(self):
        """Visible absence doctrine: qiskit missing -> REFUSED, never silent."""
        saved = qcell.HAVE_QISKIT
        qcell.HAVE_QISKIT = False
        try:
            led = Ledger(name="q")
            try:
                qcell.qrng_bits(8, seed=1)
                self.fail("expected RuntimeError with qiskit absent")
            except RuntimeError:
                pass
            led.book_refused(__import__("executor.ledger", fromlist=["TaskRequest"])
                             .TaskRequest(task_id="qcell:qrng", prompt=""),
                             "qcell", "qiskit absent — quantum ops refused, "
                             "no classical fallback pretending to be quantum")
            self.assertEqual(led.rows[-1].kind, "REFUSED")
        finally:
            qcell.HAVE_QISKIT = saved

    def test_quantum_proposal_entropy_drop_in(self):
        """GravityField can drink from the quantum stream: same seed -> same
        candidate when the field's rng is driven by qrng uniforms."""
        f1 = GravityField(dim=2, seed=77, lamp_pull=0.0, jepa_pull=0.0)
        f2 = GravityField(dim=2, seed=77, lamp_pull=0.0, jepa_pull=0.0)
        q = qcell.QCell()
        def make_rng(vals):
            def _rng():
                return int(vals.pop(0) * 0xFFFFFFFF)
            return _rng
        for f in (f1, f2):
            u = q.uniforms(4, seed=123)
            f.rng = make_rng(list(u))
        c1, c2 = f1.propose(), f2.propose()
        self.assertEqual(c1, c2)  # quantum entropy, classical reproducibility


if __name__ == "__main__":
    unittest.main()
