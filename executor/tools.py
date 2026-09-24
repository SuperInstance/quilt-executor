"""Agent tool surface (Rung 2) — any fleet cell imports these five verbs.

The tool layer never touches ledger internals directly; every verb receipts its
own operation. Fallbacks are visible: a verb that cannot complete books REFUSED.
"""
from __future__ import annotations

import asyncio
from typing import Any

from executor.ledger import Ledger, TaskRequest, TaskResult
from executor import evaluator as evaluator_mod
from executor.evaluator import Evaluator


class ExecutorTools:
    """Five verbs: tick, dispatch, score, verify, rows."""

    def __init__(self, ledger: Ledger | None = None):
        self.ledger = ledger or Ledger(name="tool-surface")
        evaluator_mod.bind_ledger(self.ledger)
        self._eval = Evaluator()

    def tick(self, note: str = "") -> dict[str, Any]:
        self.ledger.book_tick({"note": note, "via": "tool-surface"})
        return {"ticked": True, "rows": len(self.ledger.rows)}

    def dispatch(self, task_id: str, providers: list[str]) -> dict[str, Any]:
        """Thompson draw when a bandit is bound; receipt the choice."""
        bandit = getattr(self.ledger, "bandit", None)
        chosen = bandit.draw(task_id) if bandit else (providers[0] if providers else None)
        if chosen is None:
            self.ledger.book_refused(TaskRequest(task_id=task_id, prompt=""),
                                     "dispatch", "no providers")
        return {"task_id": task_id, "chosen": chosen, "candidates": providers}

    def score(self, prompt: str, output: str, provider: str = "manual",
              task_type: str = "score") -> dict[str, Any]:
        req = TaskRequest(task_id=f"tool-{len(self.ledger.rows)}", prompt=prompt,
                          task_type=task_type)
        res = TaskResult(provider=provider, output=output, latency_ms=0.0, cost_usd=0.0)
        s = asyncio.run(self._eval.evaluate(req, res))
        return {"overall": round(s.overall, 4),
                "axes": {k: getattr(s, k) for k in
                         ("correctness", "completeness", "honesty", "conciseness")}}

    def verify(self) -> dict[str, Any]:
        return {"verify": self.ledger.verify()}

    def rows(self, since: int = 0) -> dict[str, Any]:
        return {"rows": self.ledger.rows[since:], "count": len(self.ledger.rows)}
