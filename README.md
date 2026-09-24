# quilt-executor

Use the fleet as executors. Compare them continuously. Let the bandit decide who executes. Receipt what you learn.

Four layers (Casey's Universal Translator brief, executor-flavored): **providers** (Kimi API / claude / crush / reserved env-slots) → **evaluator** (quality rubric) → **decider** (Thompson Sampling per context + marginal-gain router) → **memory** (hash-chained receipt ledger, candor/MOTH-family recipe: canonical JSON + FNV-1a-64, rivals preserved, refusals hash-committed).

Plus `router/` — the **cuOpt plugin**: fleet dispatch as a 0-1 assignment LP
(max utility − λ·cost − μ·latency − ν·context_risk; one-provider-per-task,
concurrency caps, DAG precedence). CPU seeded solver today; the identical
formulation drops into NVIDIA cuOpt on GPU boxes. No GPU → visible REFUSAL row,
never a silent fallback.

```
python3 -m unittest discover -s tests -q   # 13 pins
```

Design: `research/2026-09-24-universal-executor/DESIGN.md` (kimi1 workspace).
Doctrine: a durable system's integrity is proportional to the precision of its refusals.
