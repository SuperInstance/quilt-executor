"""Quantum receipts for the gravity field — mothquantum.com substrate.

PURE MODULE: never touches the network. Callers deliver job receipts they
fetched via the mothquantum API (MothQuantumProvider in providers.py is the
live arm). Every dice draw and every chaos read cites its job: engine,
job_id, params fingerprint, result fingerprint, and the Bell S where the
engine issued one (comet-qrng attaches a CHSH witness).

Tonight's live anchors (2026-09-25, results sha-sealed outside this repo):
  comet-qrng-v1  job 82308b28  S=2.756  p(local realist)=1.7e-244  grade simulator-baseline
  otoc-echo-v1   jobs dfe0fd11 / f860c203 / 4a8154c2  seed 1234
                 lambda(disorder 0.0/0.5/1.0) = -0.1368 / -0.1247 / -0.1154  monotone

Design rule (from the field notes): floats compute, receipts cite. The
entropy BYTES are the delivered truth; dice() drains them deterministically
so the same bytes give the same stream on any substrate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .ledger import canonical_hash

# Determinism spine for the tie-break receipt — same spine as the fleet.
MULTIPLIER = 0x6D2B79F5


def fingerprint(obj) -> str:
    """The family fingerprint: canonical JSON -> FNV-1a-64, 16 hex chars."""
    return canonical_hash(obj)


@dataclass(frozen=True)
class MothJobRef:
    """One mothquantum job, cited forever. Frozen: receipts are evidence."""
    engine: str
    job_id: str
    params_fp: str          # fingerprint of the submitted params
    result_fp: str          # fingerprint of the result blob
    bell_s: Optional[float] = None
    grade: str = ""

    def cite(self) -> str:
        s = f"{self.engine}/{self.job_id[:8]} params={self.params_fp} result={self.result_fp}"
        if self.bell_s is not None:
            s += f" bell_S={self.bell_s:.4f}"
        if self.grade:
            s += f" grade={self.grade}"
        return s


class QrngEntropy:
    """Certified bytes, drained as dice. Reproducible by construction.

    Rejection sampling keeps draws unbiased: bytes that would skew the
    modulo (incomplete final block) are skipped, never folded in.
    """

    def __init__(self, data: bytes, ref: MothJobRef):
        self._data = bytes(data)
        self._pos = 0
        self.ref = ref

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    def dice(self, n: int, sides: int) -> list[int]:
        """Draw n fair ints in [0, sides). Exhaustion is a refusal, not a wrap."""
        if sides < 2:
            raise ValueError("sides must be >= 2")
        if self.remaining < n:
            raise RuntimeError(
                f"QrngEntropy exhausted: asked {n} draws, {self.remaining} bytes left "
                f"({self.ref.cite()}) — fetch another comet-qrng job, never reuse")
        block = 256 - (256 % sides)          # fair window, byte-at-a-time
        out: list[int] = []
        while len(out) < n:
            b = self._data[self._pos]
            self._pos += 1
            if b < block:
                out.append(b % sides)
        return out

    def splitmix_stream(self, count: int):
        """Reproducible 32-bit stream seeded from the certified bytes."""
        seed = int.from_bytes(self._data[:8], "big") if len(self._data) >= 8 else 0xC0CA
        x = seed & 0xFFFFFFFF
        for _ in range(count):
            x = (x + MULTIPLIER) & 0xFFFFFFFF
            z = x
            z = ((z ^ (z >> 15)) * (z | 1)) & 0xFFFFFFFF
            z ^= (z + (((z ^ (z >> 7)) * (z | 38)) & 0xFFFFFFFF)) & 0xFFFFFFFF
            yield z

    def citation(self) -> str:
        return self.ref.cite()


class OtocChaosPrior:
    """Measured chaos signature of a perturbation landscape.

    Built from a disorder -> lambda sweep (OTOC echo-decay slope). lambda
    rises monotonically with disorder on the anchor sweep, so ruggedness()
    normalizes the measured range to [0, 1]: 0 = slow-echo ordered regime
    (smooth exploration), 1 = fast-scrambling (rugged).
    """

    def __init__(self, sweep: dict, ref: MothJobRef):
        if len(sweep) < 2:
            raise ValueError("chaos prior needs >= 2 (disorder, lambda) points")
        self.sweep = {float(k): float(v) for k, v in sweep.items()}
        self.ref = ref
        self._ks = sorted(self.sweep)

    def lambda_at(self, disorder: float) -> float:
        """Linear interpolation, clamped to the measured window."""
        d = float(disorder)
        if d <= self._ks[0]:
            return self.sweep[self._ks[0]]
        if d >= self._ks[-1]:
            return self.sweep[self._ks[-1]]
        for a, b in zip(self._ks, self._ks[1:]):
            if a <= d <= b:
                t = (d - a) / (b - a)
                return self.sweep[a] + t * (self.sweep[b] - self.sweep[a])
        return self.sweep[self._ks[-1]]

    def ruggedness(self, disorder: float) -> float:
        lam = self.lambda_at(disorder)
        lo = min(self.sweep.values())
        hi = max(self.sweep.values())
        if hi == lo:
            return 0.0
        return (lam - lo) / (hi - lo)

    def citation(self) -> str:
        pts = ",".join(f"{k:g}:{v:g}" for k, v in sorted(self.sweep.items()))
        return f"otoc[{pts}] via {self.ref.cite()}"


class ChaosShapedGravity:
    """GravityField composed with certified entropy + a chaos prior.

    propose(): the field's own stream still drives geometry (its seed is
    its intentionality) — the chaos prior only shapes WHICH pull is taken:
    ordered regime -> trust lamp exploitation; rugged regime -> lean on
    jepa prediction + uniform exploration. Tie-breaks between near-equal
    archive parents are broken by certified dice, cited to the job.
    """

    def __init__(self, field, entropy: Optional[QrngEntropy] = None,
                 prior: Optional[OtocChaosPrior] = None):
        self.field = field
        self.entropy = entropy
        self.prior = prior
        self.shaped = 0
        self.tie_breaks = 0

    def propose(self, disorder: Optional[float] = None) -> tuple:
        f = self.field
        if self.prior is not None and disorder is not None:
            r = self.prior.ruggedness(disorder)
            self.shaped += 1
            u = f.rng() / 0xFFFFFFFF
            # rugged -> widen the exploration budget, narrow lamp pull
            lamp_line = f.lamp_pull * (1.0 - 0.5 * r)
            if u < lamp_line:
                return f._toward_lamp()
            if u < lamp_line + f.jepa_pull + 0.5 * r and f.jepa.pairs:
                parent = f._uniform()
                off = f.jepa.favored_offset(parent, f.rng, f.dim, f.step)
                return tuple(p + o for p, o in zip(parent, off))
            return f._uniform()
        return f.propose()

    def break_tie(self, candidates: list) -> int:
        """Certified dice pick among near-equal candidates. Cited, reproducible."""
        if not candidates:
            raise ValueError("break_tie needs >= 1 candidate")
        if self.entropy is None:
            return 0
        self.tie_breaks += 1
        return self.entropy.dice(1, len(candidates))[0]

    def provenance(self) -> dict:
        return {
            "shaped_proposals": self.shaped,
            "tie_breaks": self.tie_breaks,
            "entropy": self.entropy.citation() if self.entropy else None,
            "chaos": self.prior.citation() if self.prior else None,
        }
