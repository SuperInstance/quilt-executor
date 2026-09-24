"""Sampled laya judge — wires LayaEvaluator into the executor judge seam (SPEC v2 §6).

Only a deterministic ~10% sample of task_ids consults laya (the escalated path
costs real time); the rest run heuristics-only and SAY SO in metadata.
Sampling is by fnv1a64(task_id), so the decision is stable, auditable, and
receipted — the same id always takes the same path.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from executor.laya_evaluator import LayaEvaluator

FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
MASK64 = 0xFFFFFFFFFFFFFFFF


def _sample_bucket(task_id: str) -> int:
    h = FNV_OFFSET
    for b in task_id.encode("utf-8"):
        h = ((h ^ b) * FNV_PRIME) & MASK64
    return h % 10_000


class LayaSampledJudge:
    """Judge callable for ``Evaluator.evaluate(req, res, judge=...)``.

    sample_rate in [0, 1]; default 0.10 (one escalated consult in ten).
    Non-sampled calls return {} (no refinement) and mark metadata so the
    path is visible downstream.
    """

    def __init__(self, evaluator: Optional[LayaEvaluator] = None,
                 sample_rate: float = 0.10) -> None:
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError("sample_rate must be in [0, 1]")
        self._eval = evaluator if evaluator is not None else LayaEvaluator()
        self._rate = float(sample_rate)
        self._cut = int(round(self._rate * 10_000))

    @property
    def sample_rate(self) -> float:
        return self._rate

    def sampled(self, task_id: str) -> bool:
        return _sample_bucket(task_id) < self._cut

    async def __call__(self, request: Any, result: Any,
                       context: Optional[dict] = None) -> dict:
        task_id = getattr(request, "task_id", "unknown")
        if not self.sampled(task_id):
            md = getattr(result, "metadata", None)
            if md is not None:
                md["laya_path"] = "heuristics_only"
                md["laya_sampled"] = False
            return {}
        md = getattr(result, "metadata", None)
        if md is not None:
            md["laya_path"] = "escalated"
            md["laya_sampled"] = True
        return await self._eval(request, result, context)
