# JEV × MOTH Field Notes — what the tools are actually for

> Casey's 01:54 directive: use JEV and MOTH extensively; document what they
> helped for and what didn't. One night of genuine use, honestly reported.
> 2026-09-25, kimi1.

## Two JEVs, both real (disambiguation that cost me an hour)

1. **JEV the hosted oracle** (TypeSafe, via `quilt-jev-toolkit`): answers
   yes/no / multiple-choice / scored questions about content. Deterministic
   (<0.01 variance), ~66ms/call, adversarially robust. The house already ran
   a **canon gate over 50 repos** with it (`JEV_FLEET_GATE.md`, Sept 24):
   quilt-canary 0.70, jev-quilt 0.60, quilt-c 0.57 top the canon-p ranking.
   **Blocked tonight:** needs `TYPESAFEAI_KEY`, absent from this environment.
   Request the key from Casey — until then, JEV-oracle usage is documented
   but not exercised. This is the honest state.
2. **jeviter the iteration library** (`SuperInstance/jeviter`): homeostatic
   iteration — yield only when the world breaks a sliding boundary belief,
   and *book the silences*. Already embodied in our `GravityField`'s JevCell
   (surprise gate + quiet receipts + seed-conditioned revival).

## MOTH, as used tonight (not as read about)

Cloned `moth-cells`, ran its 27 tests (green, after discovering tests don't
self-bootstrap `src/` — need `PYTHONPATH=src`; pip install -e fixes it),
ran the demo walk, verified the sealed chain, then built a **real campaign
over quilt-executor**: AST-scanned 174 surfaces into corpus rows, terrain
with adjacency, genome `0xC0CA`, 400 ticks, probe on the hottest cell.

**The hunter starved to death.** Tick 16 boredom dormancy in
`cells.py:to_sheet`, REFUSAL rows for dormancy and starvation, energy −5231,
chain verifies. It never reached `providers.py:execute`.

### What the death taught (the insight only use gives)

- **Campaign #1 with a naive taint heuristic** (count scary strings):
  `decider.py:predicted_gain` scored taint 4 — a pure function that touches
  no I/O. The hunter starved in a landscape that lied. *A hunter campaign is
  a fitness-landscape audit: if the hunter dies in "rich" territory, the
  terrain is misdescribing danger.*
- **Campaign #2 with an honest heuristic** (I/O verbs as taint, boundary
  verbs as entry): the harbor's true surface is **one cell** —
  `providers.py:execute` (taint 1, entry 3). Everything else is pure
  functions, hashing, and the custody verifier. The hunter still starves
  because the truthful terrain is one lamp in a desert — and that *is the
  architecture review*: all kinetic force in the harbor flows through a
  single port, which is either the strongest design property or the single
  point of failure, depending on your threat model. MOTH surfaced in 400
  ticks what a day of reading might miss.

### What worked

- **Determinism as contract**: splitmix64 dice, genome_hash + dice_seed on
  every row — the walk is re-derivable, so "the hunter died" is a
  reproducible claim, not an anecdote. Same doctrine as our mulberry32 pins.
- **REFUSAL-as-data**: death, dormancy, and decoy-refusals are first-class
  rows. The two REFUSAL rows carry more architecture information than a
  passing FINDING would have.
- **witness.jsonl discipline**: append-only, chain-sealed, verify re-derives.
  A witness who rewrites testimony is a liar — the same fnv-chain idiom as
  our ledger, arrived at independently. Convergent evolution of the fleet.

### What didn't work (friction log)

- `PYTHONPATH=src` needed for tests (no self-bootstrap); `pip install -e .`
  untested, likely the intended path.
- `walk()` API differs from README sketch: returns `(hunter, sealed_rows)`
  tuple, no `ledger` kwarg, `probe_at` is a **cell index** not an id.
- My v1 taint heuristic rewarded algorithmic complexity over I/O surface —
  the tool didn't fail; *my terrain did*, and the tool was honest about it.
- Cannot run moth-runner campaigns yet (its own admission throttle deserves
  a first run with a real corpus adapter for Python — moth-corpus adapters
  exist for repos; quilt-executor would be a good first Python target).

## Designed experiments (next level, ranked)

1. **Predation-as-fitness (MOTH × GravityField).** Terrain = the pin suite;
   genome = candidate config/architecture; survival time = fitness. Evolve
   genomes that survive the test-terrain longest — search pulled by
   predation pressure instead of random mutation. *This is MothCell made
   literal.*
2. **JEV oracle as proposal judge (needs key).** Cheap canon-gate on
   gravity proposals before expensive evaluation — intentional search with
   a 66ms judge. Also: re-run the fleet gate including quilt-executor,
   gravity, qcell (all post-Sept-24, unscored).
3. **witness.jsonl admissions for gravity runs (moth-runner idiom).** Every
   proposal granted/refused with a reason; the field's stream becomes
   testimony, not just telemetry.
4. **Bell canary × QCell.** quantum_rng_comet VERIFIED mode receipts
   (S ≈ 2.83–2.90) riding the qcell backend field — provenance provably
   non-classical, one receipt per generation batch.
5. **Homeostatic scouting (jeviter idiom in my own loops).** The edge-watch
   becomes surprise-gated: baseline belief of org pushes/hour; re-scout only
   on break; book silences so "no news" is receipted no-news, not a skipped
   beat. JevCell already implements the gate — wire it to the cron.

## Verdict after one night

MOTH is an **architecture auditor wearing a predator costume** — the
hunter's death is the deliverable. JEV-oracle is a **judge we can't call
yet** (key). jeviter/JevCell is **attention economics** — react to surprise,
receipt the quiet. All three are good fits for quilt-work; the powers we
"don't fully understand" are mostly on the JEV-oracle side, and the key
unlocks that experiment.
