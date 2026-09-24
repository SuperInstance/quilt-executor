"""Gravity-guided search — evolution with fields, not lotteries.

Casey's 01:06 directive: JEV, Moth, and JEPA as non-symbolic nudging cells
that pull ML search beyond random trial-and-error into intentionality from
the seed. Three cells, one field:

- MothCell  (attraction): archive regions with high fitness are LAMPS;
  proposals drift toward light instead of wandering uniform space.
  Mirrors moth-cells' energy terrain: hunters go where the prey is dense.
- JepaCell  (direction): a predictive embedding layer — learns which offset
  directions historically improved fitness and biases proposals along them.
  Non-symbolic: it never names a feature, it pulls in vector space.
- JevCell   (surprise gate + spiral memory): homeostatic iteration in
  jeviter's idiom — the field only iterates when observations surprise the
  boundary belief; every quiet pull books a silence receipt. Its spiral log
  is the seed-conditioned gravity profile: the SAME seed revives the SAME
  field, so intentionality persists across runs instead of dying with them.

Stdlib only. mulberry32 + Box-Muller so proposals reproduce on every
substrate. Degenerate fields (empty archive, no profile) book REFUSED,
never crash silently.
"""
from __future__ import annotations

import hashlib
import math

# --------------------------------------------------------------------------
# deterministic core (fleet standard: seeds are programs, not lottery tickets)
# --------------------------------------------------------------------------


def mulberry32(seed: int):
    """Deterministic PRNG — identical streams on Python, JS, Rust, WASM."""
    state = seed & 0xFFFFFFFF

    def nxt() -> int:
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        t = state
        t = ((t ^ (t >> 15)) * (t | 1)) & 0xFFFFFFFF
        t ^= (t + ((t ^ (t >> 7)) * (t | 61))) & 0xFFFFFFFF
        return (t ^ (t >> 14)) & 0xFFFFFFFF
    return nxt


def gauss(rng) -> float:
    """Box-Muller from the seeded stream — reproducible normal draws."""
    u1 = max(rng() / 0xFFFFFFFF, 1e-12)
    u2 = rng() / 0xFFFFFFFF
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


# --------------------------------------------------------------------------
# the three cells
# --------------------------------------------------------------------------


class MothCell:
    """Attraction basins: high-fitness archive cells are lamps."""

    def __init__(self, sharpness: float = 2.0):
        self.sharpness = sharpness  # fitness ** sharpness = lamp intensity

    def weights(self, archive: dict) -> dict:
        return {k: max(c["fitness"], 0.0) ** self.sharpness
                for k, c in archive.items() if c.get("fitness", 0.0) > 0.0}


class JepaCell:
    """Predictive direction layer: learn offset→delta pairs, predict
    improvement, pull proposals along the predicted gradient."""

    def __init__(self, k: int = 5):
        self.k = k
        self.pairs: list[tuple[tuple, tuple, float]] = []  # (parent, offset, delta)

    def train(self, parent: tuple, child: tuple, delta: float) -> None:
        offset = tuple(c - p for p, c in zip(parent, child))
        self.pairs.append((parent, offset, delta))

    def predict(self, parent: tuple, offset: tuple) -> float:
        """Inverse-distance-weighted delta from k nearest trained pairs."""
        if not self.pairs:
            return 0.0
        scored = sorted(
            self.pairs,
            key=lambda po: sum((a - b) ** 2 for a, b in zip(po[0], parent)),
        )[: self.k]
        num = den = 0.0
        for _, off, delta in scored:
            d2 = sum((a - b) ** 2 for a, b in zip(off, offset))
            w = 1.0 / (d2 + 1e-9)
            num += w * delta
            den += w
        return num / den if den else 0.0

    def favored_offset(self, parent: tuple, rng, dim: int, step: float) -> tuple:
        """Sample candidate offsets, return the one the model predicts best."""
        best, best_pred = None, -math.inf
        for _ in range(8):
            cand = tuple(gauss(rng) * step for _ in range(dim))
            p = self.predict(parent, cand)
            if p > best_pred:
                best, best_pred = cand, p
        return best if best is not None else tuple(0.0 for _ in range(dim))


class JevCell:
    """Surprise gate + spiral memory (jeviter idiom: rest, and react)."""

    def __init__(self, k: float = 2.0, warm: int = 4):
        self.k = k
        self.warm = warm
        self.observed: list[float] = []
        self.quiet_pulls = 0

    def surprise(self, value: float) -> tuple[bool, float]:
        """True when |value - mean| > k·std of the boundary belief."""
        n = len(self.observed)
        if n < self.warm:
            self.observed.append(value)
            return True, 0.0
        mean = sum(self.observed) / n
        var = sum((o - mean) ** 2 for o in self.observed) / n
        dev = abs(value - mean)
        surprised = dev > self.k * math.sqrt(var)
        self.observed.append(value)
        if not surprised:
            self.quiet_pulls += 1
        return surprised, dev

    def profile_seed(self, seed: int, archive: dict, jepa: JepaCell) -> dict:
        """The gravity profile: a seed deterministically revives the field."""
        blob = repr(sorted((k, round(c["fitness"], 6))
                           for k, c in archive.items())).encode()
        blob += repr([(p, o, round(d, 6)) for p, o, d in jepa.pairs]).encode()
        return {"seed": seed, "field_digest":
                hashlib.sha256(blob).hexdigest()[:16]}


# --------------------------------------------------------------------------
# the field
# --------------------------------------------------------------------------


class GravityField:
    """Lamps + predictions + surprise, composed into a proposal distribution.

    propose(): with lamp pull — sample toward a lamp centroid;
    with jepa pull — step along the predicted-improvement offset;
    else — uniform. Same seed + same profile => same stream: intentionality
    is reproducible, not hoped for."""

    def __init__(self, dim: int, seed: int, lamp_pull: float = 0.5,
                 jepa_pull: float = 0.3, step: float = 0.1):
        self.dim = dim
        self.rng = mulberry32(seed)
        self.archive: dict = {}
        self.moth = MothCell()
        self.jepa = JepaCell()
        self.jev = JevCell()
        self.lamp_pull = lamp_pull
        self.jepa_pull = jepa_pull
        self.step = step
        self.proposals = 0

    # -- archive --------------------------------------------------------
    def light(self, key: str, centroid: tuple, fitness: float) -> None:
        self.archive[key] = {"centroid": tuple(centroid), "fitness": float(fitness)}

    # -- proposals --------------------------------------------------------
    def _uniform(self, lo: float = -1.0, hi: float = 1.0) -> tuple:
        return tuple(lo + (hi - lo) * (self.rng() / 0xFFFFFFFF)
                     for _ in range(self.dim))

    def _toward_lamp(self) -> tuple:
        weights = self.moth.weights(self.archive)
        if not weights:
            return self._uniform()
        total = sum(weights.values())
        pick = self.rng() / 0xFFFFFFFF * total
        acc = 0.0
        for key, w in weights.items():
            acc += w
            if pick <= acc:
                c = self.archive[key]["centroid"]
                return tuple(x + gauss(self.rng) * self.step for x in c)
        return self._uniform()

    def propose(self) -> tuple:
        self.proposals += 1
        u = self.rng() / 0xFFFFFFFF
        if u < self.lamp_pull:
            return self._toward_lamp()
        if u < self.lamp_pull + self.jepa_pull and self.jepa.pairs:
            parent = self._uniform()
            off = self.jepa.favored_offset(parent, self.rng, self.dim, self.step)
            return tuple(p + o for p, o in zip(parent, off))
        return self._uniform()

    # -- observation loop -------------------------------------------------
    def observe(self, candidate: tuple, fitness: float, ledger=None,
                substrate: str = "gravity") -> bool:
        """Evaluate-and-learn. Books receipts if a ledger is bound."""
        from executor.ledger import TaskRequest
        weights = self.moth.weights(self.archive)
        if not weights and not self.jepa.pairs:
            if ledger is not None:
                ledger.book_refused(TaskRequest(task_id="gravity:observe",
                                                prompt=""), substrate,
                                    "degenerate field: empty archive and no "
                                    "trained pairs; gravity needs mass")
            return False
        # nearest archive cell becomes the parent reference
        parent_key = None
        if self.archive:
            parent_key = min(self.archive,
                             key=lambda k: sum((a - b) ** 2 for a, b in
                                               zip(self.archive[k]["centroid"],
                                                   candidate)))
            parent_c = self.archive[parent_key]["centroid"]
            delta = fitness - self.archive[parent_key]["fitness"]
            self.jepa.train(parent_c, candidate, delta)
            self.archive[parent_key]["centroid"] = tuple(
                p + 0.1 * (c - p) for p, c in zip(parent_c, candidate))
        surprised, dev = self.jev.surprise(fitness)
        if ledger is not None:
            if surprised:
                ledger.book("EFFECT", {
                    "substrate": substrate, "cell_id": parent_key or "field",
                    "fitness": round(fitness, 6), "surprise_dev": round(dev, 6),
                    "proposals": self.proposals})
            else:
                ledger.book_tick({"via": substrate, "quiet": True,
                                  "quiet_pulls": self.jev.quiet_pulls})
        return surprised

    def profile(self, seed: int) -> dict:
        return self.jev.profile_seed(seed, self.archive, self.jepa)
