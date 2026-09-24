# quilt-executor

Use a fleet of LLM providers as one executor: route each task to the provider a
Thompson-sampling bandit currently trusts most, score what comes back on a fixed
rubric, and write every decision — including every refusal — into a tamper-evident
receipt ledger. Stdlib only. No services to stand up, no database, no API key
required for the sea-trial.

## Quickstart

### 1. Install

```bash
git clone https://github.com/SuperInstance/quilt-executor.git
cd quilt-executor
pip install -e .
```

> On Python ≥ 3.12, create a virtualenv first:
> `python -m venv .venv && source .venv/bin/activate`
>
> Runtime dependencies: none. The extras (`laya`, `cuopt`, `fast`, `dev`) are
> optional and only needed for their specific lanes.

### 2. Thirty-second tour — the five verbs

Every cell talks to the executor through five verbs in `executor.tools`:

```python
from executor.tools import ExecutorTools

t = ExecutorTools()
t.tick("hello")                                  # book a TICK row
t.dispatch("task-1", ["kimi", "claude"])         # choose a provider (bandit draw when bound)
t.score("name a color", "blue is a color")       # rubric score; books an EFFECT row
t.verify()                                       # re-derive the whole chain
t.rows()                                         # read the ledger
```

Each verb receipts its own operation. A verb that cannot complete books a
REFUSED row with the reason — fallbacks are visible, never silent.

### 3. Web surface

```bash
python -m executor.web        # serves http://127.0.0.1:8402
```

| Endpoint | What it does |
|---|---|
| `GET /chart` | the embedded chart (section 4) |
| `GET /ledger/export` | machine-readable chain: all rows + genesis anchor |
| `GET /ledger/rows?since=N` | raw rows, optionally from offset N |
| `GET /ledger/verify` | native chain check |
| `POST /tick` `{"note": "..."}` | book a TICK row |
| `POST /score` `{"prompt": "...", "output": "..."}` | score output, book the EFFECT row |
| `GET /stream` | newline-delimited receipt feed |
| `GET /health` | liveness + row count |

Stdlib-only HTTP server. Every request books its own receipt row for its own
operation — including failures: an exception becomes a REFUSED row with the
reason, not a silent 500.

Try it:

```bash
curl -s http://127.0.0.1:8402/chart
curl -s -X POST http://127.0.0.1:8402/tick -d '{"note": "first light"}'
curl -s -X POST http://127.0.0.1:8402/score -d '{"prompt": "name a color", "output": "blue"}'
curl -s http://127.0.0.1:8402/ledger/verify
```

### 4. The embedded chart — `ports/embedded/manifest.json`

The chart is the complete, machine-readable recipe for the receipt chain:
hash algorithm (FNV-1a-64 with its offset basis, prime, and mask), the
canonicalization rule, row schema, opcodes, substrate variants, and a
conformance vector (`café Δ 日本語`) to test your implementation against.

A **foreign vessel** — any codebase in any language, down to a microcontroller
running MicroPython — needs nothing besides this one file plus a chain export
from `GET /ledger/export` to independently re-derive every hash, verify the
whole chain, detect tampering loudly, and append rows that our substrate
re-derives and accepts. No code import, no hand-delivered constants, no fleet
knowledge. If verification required anything not in the chart, the chart would
be the bug — and the sea-trial in section 5 is built to catch exactly that.

### 5. Sea-trial

```bash
python -m pytest tests/ -q        # 77 pins
```

The suite includes the Stranger's Sea-Trial (`tests/test_foreign_vessel.py`):
a `ForeignVessel` class that imports nothing from this repo and sails using
only the chart plus an exported chain. The ocean is real only if a foreign
vessel can sail it.

Optional live demo — one real task through providers → evaluator → bandit →
ledger:

```bash
python examples/fleet_tick.py --json
```

With no API keys available it degrades to visible REFUSED rows and still exits
0; exit 1 means the chain itself broke.

### 6. Doctrine

**Receipts over self-report.** Every operation the executor performs is booked
as a row: intents (BIND), decisions with all rivals preserved (EFFECT), refusals
(REFUSED), and ticks (TICK). If it didn't receipt, it didn't happen — the ledger,
not the agent's summary, is the source of truth.

**Visible refusals.** A system that cannot say no precisely cannot be trusted
when it says yes. Every cannot-do books a REFUSED row carrying the reason,
hash-committed forever. A durable system's integrity is proportional to the
precision of its refusals.

**The chart must suffice.** All knowledge a stranger needs to verify and extend
the chain lives in `manifest.json`. Hidden knowledge is treated as a bug in the
chart, never as the stranger's problem.

## Architecture (one breath)

Four layers: **providers** (Kimi API / claude / local CLI / reserved env slots)
→ **evaluator** (fixed quality rubric) → **decider** (Thompson sampling per
context + marginal-gain routing) → **memory** (the hash-chained receipt ledger:
canonical JSON + FNV-1a-64, rivals preserved, refusals committed). `router/`
holds the cuOpt plugin — fleet dispatch as a 0-1 assignment LP (max utility −
λ·cost − μ·latency − ν·context_risk; one provider per task, concurrency caps,
DAG precedence). CPU-seeded solver today; the identical formulation drops into
NVIDIA cuOpt on GPU boxes. No GPU → a visible REFUSED row, never a silent
fallback.

Design doc: `research/2026-09-24-universal-executor/DESIGN.md`.
