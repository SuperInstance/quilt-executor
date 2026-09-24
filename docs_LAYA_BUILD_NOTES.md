# BUILD NOTES — LayaEvaluator (SPEC v2, build lane 2026-09-24)

Build tree: `/tmp/lane-laya-build/` — kimi1 installs into `/tmp/quilt-executor/`
after verification. Nothing in the repo was touched by this lane.

```
executor/laya_evaluator.py     the module (Clock/CalibrationTable/LeaseBook/
                               adapter/LayaEvaluator/seeding/verify_absence_row)
tests/test_laya_evaluator.py   15 pins, unittest, zero network
examples/laya_tick.py          fleet_tick variant, judge seam = LayaEvaluator
BUILD_NOTES.md                 this file
```

Status: **15/15 pins OK** (`python3 -m unittest discover -s tests -q` from the
build root). Demo tick runs green: real Router routing, typed timeout
absences, chain verify OK, exit 0.

---

## 1. What BINDS (verified on this box)

- **Real laya Router routing.** `LayaAdapter` tries `/tmp/laya4quilt-sync`
  first (swap-lane mandate). That tree's `laya/__init__.py` carries a merge
  conflict (`<<<<<<< HEAD` at line 15, SyntaxError) — the adapter catches it,
  removes the path from `sys.path`, and falls through to the clean tree
  `/tmp/laya4quilt`, which imports and smoke-routes. Demo evidence:
  `adapter_mode=real`, `served_by=english@convaiinnovations/laya`.
- **Routing ambiguity detection.** Ambiguous when `language_undecided` is
  truthy, or when the checkpoint is `english` while detection says
  `is_english: False`. Ambiguous rows escalate (`router_failure` absence,
  `escalated_to: judge`) — the gate gets no vote.
- **Gate table.** `CalibrationTable.lookup(task_type, checkpoint, length_bin,
  season)` — fitted strata return the fitted gate; English bootstrap
  0.55 (0.60 truncated); every cold stratum returns ESCALATE. Refits expire
  at season boundaries. Length bins on visible chars: le512 / le1024 / gt1024.
- **Typed absence rows on the ONE ledger.** Six types only
  (`noul_ambiguity, insufficient_context, ood, router_failure, truncation,
  timeout`). Rows carry visibility hashes, routing reality, trigger+value,
  resolvability, dual-clock cost, `parent_receipt_id: None` (until the slow
  path returns). Booking uses the ledger's `_next("REFUSED", body)` with the
  canonical row under `body["absence"]` — see Open seam #2.
- **Truncation precondition.** `tokens_total = ceil(chars/4)`; `max_len = 512`;
  `coverage = seen/total`; `ratio > 0.05` forces per-axis `truncation`
  absences, escalated; below tau the axis proceeds with weight `w = coverage`,
  weight receipted on the axis receipt.
- **Bandit neutrality — by construction, not by discipline.** The evaluator
  never calls `decider.update`. `record_ground_truth()` is the only update
  path. Pin 6 proves posterior table equality after an all-absence run.
- **Dual clock.** `Clock.tick_ms` chain-authoritative, `Clock.wall_ms`
  advisory. Exactly two reads of each per evaluation. ScriptedClock pins prove
  separation (tick 33 vs wall 41 in the same booked rows).
- **String-only states.** `_state` returns str; `MockAdapter.route` raises
  TypeError on non-str; the evaluator re-checks and fails loud.
- **Empty-output degenerate handoff.** Direct calls with empty output book
  per-axis `insufficient_context` (trigger `state_length`, value 0-ish) rather
  than scoring nothing as something. The outer Evaluator's own degenerate
  path still short-circuits before the judge (its REFUSAL rows book there) —
  that division is intentional.
- **15 pins**: per-axis fallback; absence schema + verify(); truncation
  precondition; cold-stratum fail-closed (table + evaluator path); bandit
  neutrality; dual-clock; lease accumulation (grant/decay/revoke/season
  expiry); string-only state; empty-Result path; reference outranks laya
  (calibration payload only for laya-served axes); routing-ambiguous
  escalate; prior skipped at refusal_worthy ≥ 0.7; bootstrap gate values;
  noul ambiguity band; predict-raise → `timeout`; end-to-end through the real
  Evaluator seam (judge metadata `used:...`, EFFECT row, chain verify).

## 2. What is MOCKED (honestly labeled)

- **Forward pass.** `LayaAdapter.predict` raises `CheckpointUnreachable` in
  real mode (no torch/GPU on this box) → per-axis `timeout` absences with
  trigger guard `checkpoint_unreachable`. The mock (`MockAdapter`,
  `model: mock-laya` in every response) scores only the visible prefix it is
  handed, so truncation is observable end to end. Tests inject the mock.
- **Router scores.** The shipped Router is weight-free: no score exists. Real
  mode emits `router_score: None`, `alternatives: []`,
  `router_confidence_calibrated: False`. Fabricating 0.97-style scores would
  re-create the shipped ECE-0.466 lie the gate table exists to kill. The mock
  emits synthetic scores with the calibrated flag still false.
- **Token estimate.** `chars/4` stands in for a tokenizer count. Every
  receipt derived from it is labeled an estimate in the module docstring; the
  GPU lane must replace it with real tokenizer counts (Open seam #6).
- **Escalation cost.** `resolvability.est_extra_cost_ms` defaults to the
  spec's 1200 ms placeholder — an estimate until slow-path telemetry prices it.
- **Season.** `season = int(tick // season_length_ticks)` — a tick-window id.
  No calendar semantics (Open seam #4).

## 3. What FM / casey must verify on GPU

1. **Real predict path.** Inject a real laya `Agent` (torch + checkpoint
   weights loadable) into `LayaAdapter.predict` — the current raise is a
   typed absence, not a failure. Verify per-checkpoint `max_len_tokens`
   real values (the 512 comes from the english checkpoint card; multilingual
   max len unverified here).
2. **Router refit.** When the weight-free Router gets scored/calibrated
   alternatives, flip `router_confidence_calibrated` per stratum and populate
   `alternatives` from the refit — until then the flag honestly stays false.
3. **ECE refit per stratum.** Feed `meta["calibration"]` payloads (shape:
   task_type, season_id, checkpoint, length_bin, raw_confidence,
   probabilities, criteria_index, route_detection, truncated) into the
   temperature refit; book via `CalibrationTable.fit(...)`. Only fitted
   windows flip; bootstrap stays a labeled loading state.
4. **Dual-run pairing.** Wire slow-path verdicts back to staged absence rows
   (`parent_receipt_id`, `escalation.slow_verdict`, `pair_delta`) and feed
   pairs into `LeaseBook.record_pair`. Lease bars: ≥200 pairs, p99 |Δ| ≤ 0.25,
   refusal rate ≤ 30%, half-life 10k ticks, per-season evidence.
5. **Prior seeding against real prior work.** `seed_priors` adds Beta
   pseudo-counts (strength 2.0) once per (task_type, provider); on a refusal
   (noul ≥ 0.7) it skips and receipt-reports. Check this doesn't fight
   whatever priors the rest of the fleet already carries.

## 4. Open seams (deliberate, listed for the installer)

1. **`parent_receipt_id` is always None at booking.** Slow-path linkage is
   not wired in this lane — the column exists, the pairing loop doesn't.
2. **Absence booking rides `Ledger._next("REFUSED", body)`.** The repo's
   `ledger.py` could not be modified from a build lane, so the typed row sits
   under `body["absence"]` on a REFUSED opcode. Recommend promoting to a
   public `book_absence(...)` on the Ledger when installing — pin 2 and pin 7
   then pin the public seam instead of the private one.
3. **Real-mode `router_score: None`.** Schema consumers must accept null; a
   future router refit replaces the nulls with honest numbers.
4. **Season is tick-derived only.** If FM wants calendar seasons or drift
   detection on top, that layer is not built here.
5. **Escalation is recorded, not dispatched.** `escalated_to: judge` marks
   the row; something else (human, cron, another agent) must actually run the
   slow path and report back.
6. **`chars_per_token = 4.0`.** Replace with real tokenizer counts when the
   tokenizer is present; until then all visibility numbers are estimates.
7. **LeaseBook is standalone.** Gate lookup does not yet consume lease
   weights — the table is pure bootstrap + refit. The lease → gate wiring is
   the next lane's job.
8. **`max_state_chars = 3000` executor-side state budget.** A cut is recorded
   in metadata (`state_cut.guard = state_length`) but never absence-typed;
   if FM wants it typed, that's a one-line policy decision.

## 5. Install notes for kimi1

- Copy `executor/laya_evaluator.py` into `/tmp/quilt-executor/executor/`,
  `tests/test_laya_evaluator.py` into `/tmp/quilt-executor/tests/`,
  `examples/laya_tick.py` into `/tmp/quilt-executor/examples/`.
- The test loader reads the build module via `importlib` with
  `QUILT_EXECUTOR_REPO` defaulting to `/tmp/quilt-executor`; after install,
  the tests can import `executor.laya_evaluator` directly (the importlib path
  keeps working too — it shadows nothing).
- `laya_tick.py` prefers the installed package, falls back to the build tree.
- Run: `python3 -m unittest discover -s tests -q` from the repo root; expect
  18 (core) + 15 (laya) OK. Then `python3 examples/laya_tick.py`.
- No new third-party dependencies. TYPE_CHECKING imports only; stdlib
  elsewhere.
