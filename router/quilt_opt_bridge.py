"""quilt-optimization bridge — fleet dispatch LP onto the sibling substrate.

Queue item #2 (2026-09-24): the LP router's formulation now has a LIVE TARGET.
SuperInstance/quilt-optimization wraps NVIDIA cuOpt as a Quilt substrate:
every solve emits an OptimizationReceipt (ACCEPT/DRIFT/REFUSE) chained via
prev_witness_id. This bridge converts DispatchTasks into that substrate's
LPProblem (binary x[task][provider], one-provider-per-task ==, provider caps,
objective = utility − λ·cost − μ·latency − ν·context_risk), solves it, and
books the substrate's witness chain INTO our ledger — two receipt families,
one dispatch decision.

Honesty doctrine, same as cuopt_absent in fleet_router:
  quilt_optimization NOT importable → book a REFUSED row naming the gap and
  return None. Never a silent CPU fallback wearing the substrate's name.
DAG precedence stays the caller's job (pass only ready tasks); an unmet
dependency is a typed error, not a silently dropped constraint.
"""
from __future__ import annotations

import json

from .fleet_router import DispatchTask, cuopt_lp

_default_substrate = None  # shared LPSubstrate: witness chain links across solves


def _import_substrate():
    from quilt_optimization import (  # type: ignore
        LPProblem, LPVariable, LPConstraint, LPObjective, LinearTerm,
        LPSubstrate,
    )
    return LPProblem, LPVariable, LPConstraint, LPObjective, LinearTerm, LPSubstrate


def build_lp_problem(tasks: list[DispatchTask], caps: dict[str, int],
                     lam: float = 1.0, mu: float = 0.001, nu: float = 0.5):
    """DispatchTask list → quilt_optimization LPProblem (binary MILP)."""
    LPProblem, LPVariable, LPConstraint, LPObjective, LinearTerm, _ = _import_substrate()
    known = {t.request.task_id for t in tasks}
    for t in tasks:
        missing = [d for d in t.depends_on if d not in known]
        if missing:
            raise ValueError(f"task {t.request.task_id}: unmet deps {missing} — "
                             "caller must pass only ready tasks (DAG seam)")
    providers = sorted({p for t in tasks for p in t.utility})
    variables = [LPVariable(name=f"x[{t.request.task_id}][{p}]", lb=0.0, ub=1.0,
                            vtype="INTEGER")
                 for t in tasks for p in t.utility]
    constraints = [
        LPConstraint(
            terms=[LinearTerm(variable=f"x[{t.request.task_id}][{p}]", coefficient=1.0)
                   for p in t.utility],
            rhs=1.0, sense="==", name=f"one_provider:{t.request.task_id}")
        for t in tasks
    ]
    constraints += [
        LPConstraint(
            terms=[LinearTerm(variable=f"x[{t.request.task_id}][{p}]", coefficient=1.0)
                   for t in tasks if p in t.utility],
            rhs=float(caps.get(p, 0)), sense="<=", name=f"cap:{p}")
        for p in providers
    ]
    objective = LPObjective(
        terms=[LinearTerm(
            variable=f"x[{t.request.task_id}][{p}]",
            coefficient=t.utility.get(p, 0.0)
                        - lam * t.est_cost.get(p, 0.0)
                        - mu * t.est_latency_ms.get(p, 0.0)
                        - nu * t.context_risk.get(p, 0.0))
            for t in tasks for p in t.utility],
        sense="MAXIMIZE")
    return LPProblem(name="fleet-dispatch", variables=variables,
                     constraints=constraints, objective=objective)


def solve_via_substrate(tasks: list[DispatchTask], caps: dict[str, int],
                        ledger, lam: float = 1.0, mu: float = 0.001,
                        nu: float = 0.5, substrate=None) -> dict[str, str] | None:
    """Solve dispatch on the quilt-optimization substrate; witness into ledger.

    Returns {task_id: provider} or None (substrate absent — REFUSED row booked).
    The cuopt_lp() formulation from fleet_router is booked alongside as the
    cross-family crosscheck: same math, two receipt vocabularies.
    `substrate` defaults to a module-level LPSubstrate so consecutive solves
    chain witnesses (prev_witness_id links forward); pass your own instance
    to isolate a chain.
    """
    from executor.ledger import TaskRequest
    global _default_substrate
    try:
        _, _, _, _, _, LPSubstrate = _import_substrate()
    except ImportError as e:
        ledger.book_refused(TaskRequest(task_id="*dispatch*", prompt=""),
                            "quilt-optimization", f"substrate absent: {e}")
        return None
    if substrate is None:
        if _default_substrate is None:
            _default_substrate = LPSubstrate(cell_id="fleet-router-cell")
        substrate = _default_substrate
    problem = build_lp_problem(tasks, caps, lam, mu, nu)
    receipt = substrate.solve(problem)
    import re
    pat = re.compile(r"^x\[(?P<tid>.+?)\]\[(?P<p>.+?)\]$")
    assignment = {}
    for var, val in receipt.payload["variable_values"].items():
        if val < 0.5:
            continue
        m = pat.match(var)
        if m:
            assignment[m.group("tid")] = m.group("p")
    ledger.book_tick({
        "solver": "quilt-optimization-substrate",
        "substrate": receipt.substrate,
        "polarity": receipt.polarity,
        "status": receipt.status,
        "witness_id": receipt.witness_id,
        "prev_witness_id": receipt.prev_witness_id,
        "chain_head": substrate.chain_head,
        "objective_value": receipt.payload["objective_value"],
        "tasks": len(tasks),
        "assigned": len(assignment),
        "formulation_sha": json.dumps(
            cuopt_lp(tasks, caps, lam, mu, nu), sort_keys=True)[:16],
    })
    return assignment
