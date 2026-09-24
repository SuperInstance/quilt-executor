# Quantum × Engineering — Ideation (inline slice, was spawn 3b227e0e, gateway timeout 2026-09-25 ~01:15)

Question: what does moth-quantum teach us about running quantum engines *reliably in production* inside quilt-executor?

## Frictions already hit first-hand (2026-09-25 02:20–02:45 live runs)

1. Cloudflare 1010 blocks urllib default UA → browser-UA required. A provider that silently 4xxs is a silent-fallback hazard.
2. Strict param schemas: out-of-range values → 422; engine params differ per engine (comet-qrng takes bytes/bits; otoc-echo takes disorder/seed).
3. Nested response shapes vary per engine — result extraction needs per-engine adapters, not one generic dig.
4. Job model: POST process → poll status → GET result. No push. Polling cadence vs 300 req/min/key quota is a real design constraint.

## Proposals (ranked, smallest first)

### E1. MothQuantumProvider hardening (DO NOW, ~half day)
- Per-engine param adapters validated client-side (422 → typed REFUSED row naming the violating param, booked, visible).
- Result extraction behind per-engine `extract_result(payload)` with conformance vector per engine (like WITNESS_ID_V1's pinned vector) — the shape proof pattern we already shipped for witness ids.
- Job cache: seal job_id + result hash into the ledger; identical params → replay sealed result, never re-spend quota. Receipts make cache hits *auditable* (a cached EFFECT row cites the original job row).
- Pins: 422 → REFUSED with param name; replay hit books row referencing original job_id; UA header pinned.

### E2. Health-test alignment with comet-qrng (cheap, high value)
quantum_rng_comet runs NIST 800-90B Repetition Count + Adaptive Proportion continuously. Our comet jobs return certified bits + CHSH. Add an RCT/APT check over our received bitstream **client-side** (stdlib) and book the health-test outcome as a TICK row. Any future degradation of the entropy source shows up in our own chain before it poisons gravity reproducibility pins.

### E3. Batch submission pattern (WATCH)
qc-parallelizer packs small circuits into wide host circuits — the analog for our usage is batching *independent* engine jobs (e.g. otoc-echo sweeps across disorder values) into one submission if mothquantum ever offers batch endpoints. Until then: sequential with sealed cache (E1) is honest and sufficient. Premature to build.

### E4. Quota governor
300 req/min/key is generous; the risk is *cost*, not rate. Book per-job cost estimate in EFFECT row body when the API exposes it; ledger becomes the spending ledger for free. Deferred until pricing surfaces in API responses.

## Doctrine note
Every friction above is a REFUSED-row shape waiting to be pinned: 422 → REFUSED(param), 1010 → REFUSED(transport), unparseable result → REFUSED(shape). The executor's existing refusal machinery is exactly the right place for quantum-engine failure modes — no separate error handling, same ledger.
