"""Thompson Sampling decider + marginal-gain router.

The missing layer: continuous comparison → routing. Per context
(task_type x provider) a Beta posterior; explore/exploit by sampling;
escalate cheap->expensive only when predicted gain clears cost.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .ledger import TaskRequest, TaskResult


@dataclass
class Arm:
    alpha: float = 1.0
    beta: float = 1.0

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def update(self, quality: float) -> None:
        if quality >= 0.5:
            self.alpha += 1.0
        else:
            self.beta += 1.0

    def sample(self, rng: random.Random) -> float:
        return rng.betavariate(self.alpha, self.beta)


class ThompsonDecider:
    """Bernoulli Thompson Sampling per context. Converges to best-per-context."""

    def __init__(self, providers: list, seed: int = 0):
        self.providers = providers
        self.by_name = {p.name: p for p in providers}
        self.arms: dict[str, dict[str, Arm]] = {}
        self.rng = random.Random(seed)

    def _context(self, request: TaskRequest) -> str:
        return request.task_type

    def _arm(self, context: str, provider: str) -> Arm:
        return self.arms.setdefault(context, {}).setdefault(provider, Arm())

    def select(self, request: TaskRequest):
        context = self._context(request)
        ok = [p for p in self.providers
              if p.estimate_cost(request) <= request.max_cost_usd
              and p.estimate_latency_ms(request) <= request.max_latency_ms
              and p.available(request)]
        if not ok:
            ok = list(self.providers)
        best, best_s = None, -1.0
        for p in ok:
            s = self._arm(context, p.name).sample(self.rng)
            if s > best_s:
                best, best_s = p, s
        return best or ok[0]

    def update(self, request: TaskRequest, provider: str, quality: float) -> None:
        self._arm(self._context(request), provider).update(quality)

    def rank(self, request: TaskRequest) -> list[tuple[str, float]]:
        context = self._context(request)
        out = [(p.name, self._arm(context, p.name).mean) for p in self.providers]
        return sorted(out, key=lambda x: -x[1])

    def posterior_table(self) -> dict:
        """The reflex table as a queryable asset — far-future users file intents here."""
        return {ctx: {p: {"alpha": a.alpha, "beta": a.beta, "mean": a.mean}
                      for p, a in arms.items()}
                for ctx, arms in self.arms.items()}


@dataclass
class MarginalGainRouter:
    """RouteLMT-style: escalate cheap->expensive only when predicted gain clears cost.

    GAIN CRITERION (v1, honest): gain = w1*posterior_gap + w2*task_risk + w3*floor_risk
      posterior_gap = expensive_mean - cheap_mean for this context (bandit's own belief)
      task_risk      = min(len(prompt)/500, 1.0)  (long tasks = more surface to fail)
      floor_risk     = 1.0 if quality_floor > cheap_mean else 0.0
    Lies when: posterior is young (n<5 both arms) — exploration noise masquerades as gap.
    Instrument: book EFFECT rows for BOTH the escalated call AND the counterfactual
    cheap-run estimate; the lie becomes measurable in the ledger. (See crush memo.)
    """
    cheap: object
    expensive: object
    decider: ThompsonDecider
    cost_threshold: float = 0.02
    gain_threshold: float = 0.15

    def predicted_gain(self, request: TaskRequest) -> float:
        ctx = request.task_type
        cheap_mean = self.decider._arm(ctx, self.cheap.name).mean
        exp_mean = self.decider._arm(ctx, self.expensive.name).mean
        cheap_n = self.decider._arm(ctx, self.cheap.name).alpha + self.decider._arm(ctx, self.cheap.name).beta - 2
        exp_n = self.decider._arm(ctx, self.expensive.name).alpha + self.decider._arm(ctx, self.expensive.name).beta - 2
        youth_discount = min(min(cheap_n, exp_n) / 5.0, 1.0)  # young posteriors → shrink the gap
        posterior_gap = max(exp_mean - cheap_mean, 0.0) * youth_discount
        task_risk = min(len(request.prompt) / 500.0, 1.0) * 0.3
        # floor risk needs EVIDENCE too: an uninformed prior (n<5) cannot trigger it
        floor_risk = (0.2 if request.quality_floor > cheap_mean else 0.0) * youth_discount
        return posterior_gap + task_risk + floor_risk

    def should_escalate(self, request: TaskRequest) -> bool:
        cost_delta = (self.expensive.estimate_cost(request)
                      - self.cheap.estimate_cost(request))
        if cost_delta > self.cost_threshold:
            return False
        return self.predicted_gain(request) > self.gain_threshold
