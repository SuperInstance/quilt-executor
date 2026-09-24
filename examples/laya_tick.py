"""A laya-routed perception tick (SPEC v2): one small real task flows through
the executor with LayaEvaluator sitting in the judge seam.

providers -> evaluate(judge=LayaEvaluator) -> seed priors -> decider update
(ground truth only) -> ledger -> verify.  Machine-speed perception: the laya
forward pass (mock on this box, real Router routing when importable) costs
microseconds against the provider's seconds.  Keyless-honest: with no API key
the tick degrades to visible REFUSAL rows and still exits 0 — the perception
organ reports its own blindness honestly instead of going dark silently.

Usage: python3 examples/laya_tick.py [--json]
Exit code is always 0 unless the CHAIN ITSELF breaks (verify failure = 1).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

BUILD_ROOT = Path(__file__).resolve().parent.parent
REPO = Path(os.environ.get("QUILT_EXECUTOR_REPO", BUILD_ROOT.parent / "quilt-executor"))
sys.path.insert(0, str(REPO))

from executor.ledger import Ledger, TaskRequest
from executor.providers import KimiProvider, CliProvider
from executor.evaluator import Evaluator
from executor.decider import ThompsonDecider
import executor.evaluator as ev

try:  # installed package first
    from executor.laya_evaluator import (  # type: ignore
        CalibrationTable, LayaEvaluator, LayaAdapter, SystemClock, seed_priors,
    )
except ImportError:  # build-tree fallback (kimi1 installs after verification)
    sys.path.insert(0, str(BUILD_ROOT))
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "executor.laya_evaluator", BUILD_ROOT / "executor" / "laya_evaluator.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["executor.laya_evaluator"] = _mod  # dataclass annotation lookup
    _spec.loader.exec_module(_mod)
    CalibrationTable = _mod.CalibrationTable
    LayaEvaluator = _mod.LayaEvaluator
    LayaAdapter = _mod.LayaAdapter
    SystemClock = _mod.SystemClock
    seed_priors = _mod.seed_priors


TICK_PROMPT = ("In one line, name one concrete way a fleet of AI agents could "
               "measure whether another agent's work output is trustworthy.")


def run_tick() -> dict:
    ledger = Ledger(name="laya-tick")
    request = TaskRequest(
        task_id=f"laya-tick-{int(time.time())}",
        prompt=TICK_PROMPT,
        task_type="perception",
        max_cost_usd=0.01,
        max_latency_ms=30_000,
    )
    ledger.book_bind(request)

    providers = [KimiProvider(max_tokens=128), CliProvider("crush", "crush")]
    decider = ThompsonDecider(providers, seed=int(time.time()) // 3600)
    ev.bind_ledger(ledger)

    judge = LayaEvaluator(
        gate_table=CalibrationTable(),
        clock=SystemClock(),
        adapter=LayaAdapter(),  # real Router when importable, mock forward pass
    )

    chosen = decider.select(request)
    ok, why = chosen.available(request)
    if not ok:
        ledger.book_refused(request, chosen.name, why)
    result = chosen.execute(request)

    score = asyncio.run(Evaluator().evaluate(request, result, judge=judge))

    # §4: laya SEEDS (prior) and PROPOSES (feature); ground truth UPDATES.
    laya_meta = result.metadata.get("laya", {})
    seeded: set = set()
    seed_receipt = seed_priors(
        decider, request, result.provider,
        {axis: d["value"] for axis, d in (laya_meta.get("axes") or {}).items()},
        (laya_meta.get("refusal_worthy") or {}).get("noul"),
        _seeded=seeded,
    )
    decider.update(request, result.provider, score.overall)  # heuristic floor truth

    chain_ok, problems = ledger.verify()
    laya_route = laya_meta.get("route") or {}
    backends = {}
    for r in laya_meta.get("receipts", []):
        if "axis" in r:
            backends[r["axis"]] = r["backend"]

    summary = {
        "task_id": request.task_id,
        "provider": result.provider,
        "quality": round(score.overall, 4),
        "latency_ms": round(result.latency_ms, 1),
        "laya": {
            "adapter_mode": laya_meta.get("adapter_mode"),
            "served_by": laya_meta.get("served_by"),
            "checkpoint": laya_route.get("checkpoint_id"),
            "gate": (laya_meta.get("gate") or {}).get("value"),
            "truncation": laya_meta.get("truncation"),
            "axes": laya_meta.get("axes"),
            "backends": backends,
            "absences": [a["absence_type"] for a in laya_meta.get("absences", [])],
            "cost": laya_meta.get("cost"),
        },
        "seed_receipt": seed_receipt,
        "ledger_rows": len(ledger.rows),
        "refusals": len(ledger.refusals()),
        "chain_ok": chain_ok,
        "problems": problems,
        "output_preview": (result.output or result.error or "")[:160],
        "posterior": decider.posterior_table().get("perception", {}),
    }
    ledger.book_tick({"summary_keys": sorted(summary.keys())})
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="print raw summary JSON")
    args = ap.parse_args()
    s = run_tick()
    if args.json:
        print(json.dumps(s, indent=2, ensure_ascii=False))
    else:
        l = s["laya"]
        print(f"tick {s['task_id']}: provider={s['provider']} "
              f"quality={s['quality']} refusals={s['refusals']} "
              f"chain_ok={s['chain_ok']}")
        print(f"  laya[{l['adapter_mode']}] checkpoint={l['checkpoint']} "
              f"gate={l['gate']} absences={len(l['absences'])} "
              f"cost_tick_ms={(l['cost'] or {}).get('tick_ms')}")
        print(f"  backends={l['backends']}")
        if s["seed_receipt"]:
            print(f"  seed={s['seed_receipt']['kind']}")
        print(f"  {s['output_preview']}")
    sys.exit(0 if s["chain_ok"] else 1)
