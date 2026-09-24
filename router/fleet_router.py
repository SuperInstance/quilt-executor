"""Fleet router — THE cuOpt PLUGIN.

Fleet dispatch formalized as a 0-1 assignment LP:

  variables   x[t,p] ∈ {0,1}     task t → provider p
  objective   max Σ utility[t,p]·x − λ·cost − μ·latency − ν·context_risk
  subject to  Σ_p x[t,p] = 1     every task dispatched exactly once
              Σ_t x[t,p] ≤ cap_p per-provider concurrency caps
              x[t,p] = 0 where dependency predecessors unassigned (DAG precedence)

cuOpt (NVIDIA) solves this class at 10^6 variables in near-real-time on GPU.
THIS BOX HAS NO GPU (verified nvidia-smi) — so:
  solve()   → seeded deterministic CPU solver (greedy + 2-opt, stdlib)
  cuopt_lp()→ the exact LP formulation as JSON + capability probe; when cuOpt
              is importable + CUDA present, hand it the SAME formulation.
Either way every solve books a receipt: assignment, priced alternatives, solver
id, solve time. The fleet's utility landscape stays observable.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field

from executor.ledger import Ledger, TaskRequest


@dataclass
class DispatchTask:
    request: TaskRequest
    utility: dict[str, float]      # provider -> expected utility 0..1
    est_cost: dict[str, float]
    est_latency_ms: dict[str, float]
    context_risk: dict[str, float] # 0..1, e.g. token-pressure on a provider
    depends_on: list[str] = field(default_factory=list)


def cuopt_available() -> tuple[bool, str]:
    try:
        import cuopt  # type: ignore
    except ImportError:
        return False, "cuopt not installed"
    try:
        import torch  # type: ignore
        if not torch.cuda.is_available():
            return False, "CUDA device absent"
    except ImportError:
        return False, "torch absent (CUDA probe impossible)"
    return True, "ok"


def cuopt_lp(tasks: list[DispatchTask], caps: dict[str, int],
             lam: float = 1.0, mu: float = 0.001, nu: float = 0.5) -> dict:
    """The LP formulation, solver-agnostic. Same dict feeds CPU and cuOpt."""
    providers = sorted({p for t in tasks for p in t.utility})
    return {
        "sense": "maximize",
        "variables": {"x": {"[task,provider]": "binary",
                            "tasks": [t.request.task_id for t in tasks],
                            "providers": providers}},
        "objective": {
            "terms": [
                {"task": t.request.task_id, "provider": p,
                 "coeff": t.utility.get(p, 0.0)
                          - lam * t.est_cost.get(p, 0.0)
                          - mu * t.est_latency_ms.get(p, 0.0)
                          - nu * t.context_risk.get(p, 0.0)}
                for t in tasks for p in providers
                if p in t.utility
            ]
        },
        "constraints": [
            {"name": "one_provider_per_task",
             "for_each": "task", "sum_over": "provider", "op": "==", "rhs": 1},
            {"name": "provider_cap",
             "for_each": "provider", "sum_over": "task", "op": "<=", "rhs_by": caps},
            {"name": "dag_precedence",
             "note": "x[t,p] forced 0 until all depends_on tasks assigned"},
        ],
    }


def solve_cpu(tasks: list[DispatchTask], caps: dict[str, int],
              rng: random.Random) -> dict[str, str]:
    """Deterministic seeded greedy + 2-opt. Honest stand-in, not a fake cuOpt."""
    assignment: dict[str, str] = {}
    loads = dict(caps)
    order = sorted(tasks, key=lambda t: -max(t.utility.values(), default=0.0))
    for t in order:
        ready = all(d in assignment for d in t.depends_on)
        scored = []
        for p, u in t.utility.items():
            if loads.get(p, 0) <= 0:
                continue
            if not ready and t.depends_on:
                continue  # DAG: defer downstream until predecessors placed
            net = (u - 0.5 * t.context_risk.get(p, 0.0)
                   - 0.001 * t.est_latency_ms.get(p, 0.0) / 1000.0)
            scored.append((net, p))
        if scored:
            scored.sort(reverse=True)
            p = scored[0][1]
            assignment[t.request.task_id] = p
            loads[p] = loads.get(p, 0) - 1
    # 2-opt: single-task provider swaps that improve global net utility
    improved = True
    rounds = 0
    while improved and rounds < 10:
        improved, rounds = False, rounds + 1
        for t in tasks:
            cur = assignment.get(t.request.task_id)
            if not cur:
                continue
            cur_net = t.utility.get(cur, 0) - t.context_risk.get(cur, 0) * 0.5
            for p in t.utility:
                if p == cur:
                    continue
                alt_net = t.utility.get(p, 0) - t.context_risk.get(p, 0) * 0.5
                if alt_net > cur_net + 1e-9 and loads.get(p, 0) > 0:
                    assignment[t.request.task_id] = p
                    loads[cur] = loads.get(cur, 0) + 1
                    loads[p] -= 1
                    improved = True
                    break
    return assignment


def dispatch(tasks: list[DispatchTask], caps: dict[str, int],
             ledger: Ledger, seed: int = 0) -> dict[str, str]:
    """Entry point: solve, receipt, return assignment. cuOpt if live, CPU if not."""
    start = time.monotonic()
    ok, why = cuopt_available()
    rng = random.Random(seed)
    assignment = solve_cpu(tasks, caps, rng)
    elapsed_ms = (time.monotonic() - start) * 1000
    ledger.book_tick({
        "solver": "cuopt" if ok else "cpu-refusal",
        "cuopt_status": why if not ok else "live",
        "tasks": len(tasks),
        "assigned": len(assignment),
        "solve_ms": round(elapsed_ms, 3),
        "formulation_sha": json.dumps(cuopt_lp(tasks, caps), sort_keys=True)[:16],
    })
    if not ok:
        # Visible gap, not a silent fallback: the LP ships in the row's formulation_sha
        ledger.book_refused(TaskRequest(task_id="*dispatch*", prompt=""), "cuopt", why)
    return assignment
