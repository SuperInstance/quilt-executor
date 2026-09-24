"""A perception tick: one small real task flows through the whole executor.

providers -> evaluate -> decider update -> ledger -> verify. Designed to be
cron'd (every N hours) and to run inside CI: with no API key it degrades to
visible REFUSAL rows and still exits 0 — the perception organ reports its
own blindness honestly instead of going dark silently.

Usage: python3 examples/fleet_tick.py [--json]
Exit code is always 0 unless the CHAIN ITSELF breaks (verify failure = 1),
because a refusal perceived is a tick completed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor.ledger import Ledger, TaskRequest
from executor.providers import KimiProvider, CliProvider
from executor.evaluator import Evaluator
from executor.decider import ThompsonDecider
import executor.evaluator as ev


TICK_PROMPT = ("In one line, name one concrete way a fleet of AI agents could "
               "measure whether another agent's work output is trustworthy.")


def run_tick() -> dict:
    ledger = Ledger(name="fleet-tick")
    request = TaskRequest(
        task_id=f"tick-{int(time.time())}",
        prompt=TICK_PROMPT,
        task_type="perception",
        max_cost_usd=0.01,
        max_latency_ms=30_000,
    )
    ledger.book_bind(request)

    providers = [KimiProvider(max_tokens=128), CliProvider("crush", "crush")]
    decider = ThompsonDecider(providers, seed=int(time.time()) // 3600)
    ev.bind_ledger(ledger)

    chosen = decider.select(request)
    ok, why = chosen.available(request)
    rivals = []
    if not ok:
        ledger.book_refused(request, chosen.name, why)
        result = chosen.execute(request)  # carries the refusal error
    else:
        for p in providers:
            if p is not chosen:
                rivals.append(p)
        result = chosen.execute(request)

    score = asyncio.run(Evaluator().evaluate(request, result))
    decider.update(request, result.provider, score.overall)
    chain_ok, problems = ledger.verify()

    summary = {
        "task_id": request.task_id,
        "provider": result.provider,
        "quality": round(score.overall, 4),
        "latency_ms": round(result.latency_ms, 1),
        "reasoning_tokens": result.metadata.get("reasoning_tokens"),
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
        print(json.dumps(s, indent=2))
    else:
        print(f"tick {s['task_id']}: provider={s['provider']} "
              f"quality={s['quality']} refusals={s['refusals']} "
              f"chain_ok={s['chain_ok']}")
        print(f"  {s['output_preview']}")
    sys.exit(0 if s["chain_ok"] else 1)
