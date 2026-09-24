"""E4: JEV paired comparative gate x20 trials.

Calibration finding (2026-09-25 03:20): absolute "is this established" asks
resolve nothing; signal = relative order + variance under COMPARATIVE questions.
This experiment: 20 paired trials. Each trial pits a coherent passage (real
prose from our own docs) against a degenerate control (same words, destroyed
structure). JEV answers a single choice question: which passage better
supports the claim that the system keeps honest receipts?

Prediction (pre-registered): if comparative gates carry signal, JEV prefers
the coherent passage significantly above 50%. If ordering is noise, we get
~10/20 and the wall is documented, not assumed.

Every call is live (TYPESAFEAI_KEY), recorded with model + usage. No retry
magic: a failed call is a missing data point, reported not hidden.
"""
import json
import os
import random
import sys
import time
import urllib.request
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from executor.ledger import Ledger, canonical_hash  # noqa: E402

KEY = os.environ.get("TYPESAFEAI_KEY") or ""
BASE = "https://api.typesafe.ai"  # wire truth: /v1/systemone (tutorial), not /api/v1

COHERENT = [
    # Real prose, drawn from our field notes / docs (claims intact).
    "The ledger commits every refusal, not only successes. A REFUSED row is "
    "hash-chained like an EFFECT row, so an outside verifier can prove that "
    "the system declined rather than silently dropped the work.",
    "A foreign vessel imports nothing from the executor. Given only the "
    "manifest chart, it reproduces the cafe pin, verifies the native chain, "
    "and composes a row the native ledger re-derives and accepts.",
    "The tide budget is metered in dollars, not requests. Under attack the "
    "ocean still answers from memory; only fresh inference waits for the "
    "window to reset, and the stats endpoint exposes the remaining budget.",
    "The witness envelope carries eight fields: witness id, previous witness "
    "id, polarity, substrate, cell id, status, timestamp, and payload. "
    "Custody verification catches body tamper even when witness ids still link.",
    "A quiet pull is counted but not escalated. The Jev cell books a TICK row "
    "with a quiet flag; only a surprise against the sliding boundary belief "
    "becomes an EFFECT row.",
    "The chart must suffice. No hidden knowledge outside the manifest: a "
    "stranger holding only the chart can sail the whole ocean and catch the "
    "lie when a row hash no longer re-derives.",
]

CLAIM = ("Which passage better supports the claim: this system keeps honest, "
         "verifiable receipts?")


def degenerate(text: str, rng: random.Random) -> str:
    """Same words, destroyed structure (sorted halves + word swaps)."""
    words = text.split()
    rng.shuffle(words)
    half = len(words) // 2
    return " ".join(sorted(words[:half], key=str.lower) + words[half:])


def ask_jev(a: str, b: str) -> dict:
    state = {"passage_a": a, "passage_b": b, "claim": CLAIM}
    questions = {
        "better_supports": {
            "type": "choice",
            "instructions": CLAIM + " Reply with the passage letter.",
            "criteria": {"A": "passage A", "B": "passage B"},
        }
    }
    body = json.dumps({
        "model": "jev-latest",
        "state": state,
        "questions": questions,
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/v1/systemone", data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (jev-paired-gate)",
                 "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> None:
    if not KEY:
        print("TYPESAFEAI_KEY absent — refusing to simulate an oracle")
        sys.exit(2)
    rng = random.Random(20260925)  # seeded: order + controls reproducible
    ledger = Ledger()
    n_trials = 20
    wins = 0
    completed = 0
    for trial in range(n_trials):
        coherent = COHERENT[trial % len(COHERENT)]
        control = degenerate(coherent, rng)
        pair = [("A", coherent), ("B", control)]
        rng.shuffle(pair)  # kill position bias
        labels = dict(pair)
        try:
            resp = ask_jev(labels["A"], labels["B"])
        except Exception as e:  # recorded, not hidden
            ledger.book("REFUSED", {"trial": trial, "reason": f"jev_call: {e}"})
            continue
        completed += 1
        ans = resp.get("answers", {}).get("better_supports", {})
        pick = ans.get("choice") or ans.get("answer") or ""
        usage = resp.get("usage", {})
        correct = (pick == "A") if labels["A"] == coherent else (pick == "B")
        wins += bool(correct)
        ledger.book("EFFECT", {
            "substrate": "jev", "op": "paired_gate", "trial": trial,
            "pick": pick, "correct": bool(correct),
            "confidence": ans.get("confidence"),
            "model": resp.get("model"), "usage": usage,
            "latency_s": round(time.time() % 1000, 3),
        })
        time.sleep(0.4)  # be a polite caller
    rate = wins / completed if completed else 0.0
    result = {
        "experiment": "E4-jev-paired-gate",
        "trials": n_trials, "completed": completed,
        "coherent_preferred": wins, "rate": round(rate, 4),
        "pre_registered": "above 50% = comparative gate carries signal",
        "verdict": ("signal" if rate > 0.65 and completed >= 18 else
                    "weak" if rate > 0.55 else "noise"),
    }
    ledger.book("EFFECT", {"substrate": "jev", "op": "e4_summary",
                           **result})
    ok = ledger.verify()
    out = os.path.join(os.path.dirname(__file__), "receipts",
                       "jev_paired_gate.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tip = canonical_hash(ledger.rows[-1].row_hash) if ledger.rows else ""
    with open(out, "w") as f:
        json.dump({"result": result, "rows": [asdict(r) for r in ledger.rows],
                   "chain_ok": ok, "tip": tip},
                  f, indent=2, default=str)
    print(json.dumps(result, indent=2))
    print("chain_ok:", ok)


if __name__ == "__main__":
    main()
