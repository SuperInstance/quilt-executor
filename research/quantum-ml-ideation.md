# Quantum × ML — Ideation (inline slice, was spawn 62741fa7, gateway timeout 2026-09-25 ~01:15)

Question: which moth-quantum repos change how GravityField/QCell *search*, not just how they sample?

## Repo map (evidence, /tmp/moth-*)

- **fourier_locking_study** (arXiv 2607.11013, Topel 2026): re-uploading classifiers trained naively lock onto a low Fourier band = exactly the local-optimum failure mode of seeded search. Escape = **spectral homotopy**: start training at a smoothed (low-frequency) target, anneal toward the true objective.
- **mqa-data** (Mirror Quantum Awesomeness): MRB variant tracking per-edge correlation dynamics + **critical depth** beyond which error mitigation fails. Gives a measurable "depth = capability frontier" dial per backend.
- **qc-parallelizer**: packs many small circuits into one wide host circuit — a throughput primitive, not a learning one.
- **quantum_rng_comet**: CHSH-verified QRNG w/ NIST 800-90B health tests; already consumed via comet-qrng-v1 engine + MothQRNGBackend design.

## The proposal that survives scrutiny: Spectral-Homotopy Gravity

Our JepaCell predicts `offset → delta` with kNN over a tiny archive; nothing smooths the *objective landscape* itself. The Fourier-locking result says: train/evaluate against a sequence of smoothed objectives (homotopy parameter λ: 0 = low-pass version of fitness, 1 = true fitness), annealing λ as the archive grows.

Concrete, smallest build (quantum-ml-homotopy lane):
1. `gravity.py`: `GravityField.propose()` gains optional `homotopy(lambda_)` — fitness used for lamp/jepa scoring is `(1-λ)·smooth(fitness) + λ·fitness`, where smooth = moving average over archive generation. λ anneals 0→1 with archive mass (already tracked).
2. Pins: (a) on a double-funnel synthetic fitness, homotopy-on escapes the near funnel where homotopy-off stalls (both seeded, mulberry32); (b) λ=1 exactly reproduces current behavior (bit-exact proposals); (c) receipts record λ in the EFFECT row body — the homotopy schedule is itself auditable history.
3. Honesty: simulator-grade claim only; no quantum hardware claim. The quantum tie is the *idea* (spectral homotopy), not hardware execution.

## Ranked next (not this slice)
2. **MQA critical-depth dial**: when QCell moves to real QPU (qpu_instance/qpu_token exist), use mqa-data's critical-depth estimate to set circuit depth budgets per backend; receipt records `depth_budget_source`.
3. **OtocChaosPrior → homotopy interaction**: we already have OTOC decay λ(disorder) as a ruggedness prior; test whether high-ruggedness arenas warrant slower λ annealing (pair with sprint-quantum-002's comparative gate).
4. qc-parallelizer: only if GravityField ever needs batched circuit eval (many proposals × kernel fidelity) — premature now.

## Anti-proposal (checked, rejected)
"Quantum kernel SVM for JepaCell": qcell.kernel() already gives fidelity-based pulls; a full QSVM adds fit/predict state with no evidence our archive sizes (tens of rows) exceed kNN capacity. Refused: complexity without a pinned failure of the incumbent.
