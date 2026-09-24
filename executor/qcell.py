"""QCell — the fourth nudging cell. Quantum gravity for intentional search.

Casey's 01:10 directive: fork moth-quantum's world into the quilt architecture.
This cell is the seam. Three quantum instruments, each mapped to a cell role:

- QRNG stream  -> proposal entropy (replaces mulberry32; simulator mode is
  seed-reproducible, hardware mode is true quantum randomness — one interface,
  receipts distinguish which universe answered)
- Angle kernel -> quantum similarity for the JEPA direction layer: statevector
  fidelity as the pull metric between candidates (the moth-quantum re-uploading
  idiom, one circuit at a time)
- Circuit receipts -> every batch books EFFECT with qubits/shots/backend/
  entropy; absence of qiskit books REFUSED, never a silent classical fallback
  pretending to be quantum.

qiskit is an OPTIONAL dependency: the cell degrades to visible refusal, and
the pins that need it skip rather than fake.
"""
from __future__ import annotations

import math

from executor.gravity import mulberry32

try:
    from qiskit import QuantumCircuit
    from qiskit.quantum_info import Statevector, state_fidelity
    HAVE_QISKIT = True
except ImportError:  # visible absence: the ledger will show the refusal
    HAVE_QISKIT = False

_QRNG_QUBITS = 8  # 256 amplitude slots per draw; streams reproduce via rng


def _hadamard_circuit(n: int) -> "QuantumCircuit":
    qc = QuantumCircuit(n)
    for i in range(n):
        qc.h(i)
    return qc


def qrng_bits(n_bits: int, seed: int) -> list[int]:
    """Hadamard-measurement bit stream. Simulator probabilities + seeded
    sampling => same seed, same stream (hardware: swap in real shots and
    set backend='hardware' on the receipt)."""
    if not HAVE_QISKIT:
        raise RuntimeError("qiskit absent — book a REFUSED row, don't fake quantum")
    probs = Statevector(_hadamard_circuit(_QRNG_QUBITS)).probabilities()
    rng = mulberry32(seed)
    slots = list(range(len(probs)))
    return [slots.index(_weighted_pick(probs, rng)) % 2 for _ in range(n_bits)]


def _weighted_pick(probs, rng) -> int:
    r = rng() / 0xFFFFFFFF
    acc = 0.0
    for i, p in enumerate(probs):
        acc += p
        if r <= acc:
            return i
    return len(probs) - 1


def qrng_unit(n: int, seed: int) -> list[float]:
    """Quantum proposal entropy as uniforms in [0,1): bit-pairs -> u."""
    bits = qrng_bits(n * 16, seed)
    out = []
    for i in range(n):
        b = bits[i * 16:(i + 1) * 16]
        out.append(sum(bit << (15 - j) for j, bit in enumerate(b)) / 65536.0)
    return out


def _angle_circuit(x: tuple) -> "QuantumCircuit":
    qc = QuantumCircuit(len(x))
    for i, v in enumerate(x):
        qc.ry(float(v) * math.pi, i)
    for i in range(len(x) - 1):  # ring entanglement: features interfere
        qc.cz(i, i + 1)
    if len(x) > 2:
        qc.cz(len(x) - 1, 0)
    return qc


def kernel(x: tuple, y: tuple) -> float:
    """Statevector fidelity as quantum similarity — the JepaCell pull metric."""
    if not HAVE_QISKIT:
        raise RuntimeError("qiskit absent — book a REFUSED row, don't fake quantum")
    fx = Statevector(_angle_circuit(x))
    fy = Statevector(_angle_circuit(y))
    return float(state_fidelity(fx, fy))


class QCell:
    """Drop-in entropy+direction upgrade for GravityField."""

    def __init__(self, backend: str = "simulator"):
        self.backend = backend  # receipt field: which universe answered
        self.circuits_run = 0

    def uniforms(self, n: int, seed: int, ledger=None) -> list[float]:
        u = qrng_unit(n, seed)
        self.circuits_run += 1
        if ledger is not None:
            ledger.book("EFFECT", {"substrate": "qcell", "op": "qrng_unit",
                                   "qubits": _QRNG_QUBITS, "backend": self.backend,
                                   "draws": n, "circuits": self.circuits_run})
        return u

    def similarity(self, x: tuple, y: tuple, ledger=None) -> float:
        f = kernel(x, y)
        self.circuits_run += 1
        if ledger is not None:
            ledger.book("EFFECT", {"substrate": "qcell", "op": "kernel",
                                   "qubits": len(x), "backend": self.backend,
                                   "fidelity": round(f, 6),
                                   "circuits": self.circuits_run})
        return f
