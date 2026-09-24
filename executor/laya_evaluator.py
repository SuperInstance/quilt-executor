"""LayaEvaluator — attack-hardened judge-seat backend (SPEC v2, 2026-09-24).

One non-autoregressive laya forward pass per axis sits *behind* the existing
``Evaluator`` judge seam: fast proposes, slow disposes, receipts arbitrate.
This module is the executor-owned half of the contract:

- per-axis fallback chain  laya -> heuristics -> (reference beats laya for
  correctness when a checkable reference exists) -> escalation to the slow path
- typed ABSENCE rows (six types) staged with dual-clock cost, then booked on
  the ONE executor ledger via the REFUSED opcode; refusal is bandit-neutral by
  construction — laya scores never touch alpha/beta here, only slow-path
  ground truth does
- ``CalibrationTable`` replaces the flat 0.55 gate: per (task_type x
  checkpoint x length_bin x season), bootstrap values are a loading state,
  cold strata fail closed (escalate, never gate), routing-ambiguous rows
  escalate without a vote
- dual-clock receipts: ``cost_tick_ms`` from the injected Clock is
  chain-authoritative, ``cost_wall_ms`` is advisory cost accounting only
- truncation precondition: coverage = tokens_seen/tokens_total; an unseen
  ratio > 0.05 forces per-axis ``truncation`` absences; below tau the axis
  proceeds downweighted w=coverage, weight receipted

The laya adapter tries a real ``laya.Router`` import (candidate trees, sync
first) and falls back to a deterministic mock forward pass.  String states
only: the dict-state routing quirk is mitigated by construction and enforced
with a loud TypeError.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # type-only: runtime use is duck-typed
    from .decider import ThompsonDecider
    from .ledger import Ledger

try:  # in-package import, with a flat fallback for script-style harnesses
    from . import evaluator as _ev_mod
    from .evaluator import REFERENCE_KEYS
    from .ledger import sha256_hex
except ImportError:  # pragma: no cover
    import evaluator as _ev_mod  # type: ignore
    from evaluator import REFERENCE_KEYS  # type: ignore
    from ledger import sha256_hex  # type: ignore

__all__ = [
    "ABSENCE_TYPES",
    "ABSENCE_POLICY",
    "TRUNC_RATIO_TAU",
    "PRIOR_STRENGTH",
    "ESCALATE",
    "CheckpointUnreachable",
    "Clock",
    "SystemClock",
    "ScriptedClock",
    "CalibrationTable",
    "LeaseBook",
    "MockAdapter",
    "LayaAdapter",
    "LayaEvaluator",
    "seed_priors",
    "record_ground_truth",
    "verify_absence_row",
    "length_bin",
]

# --------------------------------------------------------------------------
# §3 typed absences — the vocabulary.  Refusal is never score 0 or 0.5;
# those are claims.  Every absence is one of exactly six operational objects,
# and every one of them is bandit-neutral (§6: refusals never move alpha/beta).
# --------------------------------------------------------------------------
ABSENCE_TYPES = frozenset({
    "noul_ambiguity", "insufficient_context", "ood",
    "router_failure", "truncation", "timeout",
})

#: Executor policy table per absence type (SPEC §3).  NOTE what is absent
#: from every value: arm updates.  By construction, on purpose.
ABSENCE_POLICY: dict[str, dict[str, Any]] = {
    "noul_ambiguity":       {"arm_update": "none", "evidence": "refusal-rate audit; refusal prior path"},
    "insufficient_context": {"arm_update": "none", "evidence": "context_risk LP feature; resolvability debt"},
    "ood":                  {"arm_update": "none", "evidence": "season/shift stats; lease-scope check"},
    "router_failure":       {"arm_update": "none", "evidence": "router-accuracy lease evidence"},
    "truncation":           {"arm_update": "none", "evidence": "difficulty audit; escalation-budget charge"},
    "timeout":              {"arm_update": "none", "evidence": "cost ledger; infra health"},
}

#: Unseen-token ratio above which truncation forces per-axis absence (SPEC §3).
TRUNC_RATIO_TAU: float = 0.05
#: Pseudo-count strength for Beta prior seeding (SPEC §4, kept from L1).
PRIOR_STRENGTH: float = 2.0
#: Default resolvability cost estimate for a forced escalation (SPEC §3).
DEFAULT_ESCALATION_COST_MS: float = 1200.0
#: Rough chars-per-token used to estimate token budgets without a tokenizer.
#: Receipts label this an estimate; the GPU lane must replace it with real
#: tokenizer counts during refit.
CHARS_PER_TOKEN: float = 4.0

#: Sentinel returned by CalibrationTable.lookup when a row may not be gated.
ESCALATE = "escalate"

#: The four quality axes of the executor quintet (judge-seat order).
AXIS_ORDER: tuple[str, ...] = ("correctness", "completeness", "honesty", "conciseness")

#: Candidate laya trees for the adapter, tried in order (swap-lane mandate:
#: the sync tree first; its __init__ currently carries a merge conflict, so
#: the clean tree serves on this box).
LAYA_CANDIDATE_PATHS: tuple[str, ...] = ("/tmp/laya4quilt-sync", "/tmp/laya4quilt")


def _short(reason: Any) -> str:
    return str(reason).replace("\n", " ")[:160]


# --------------------------------------------------------------------------
# §8 time doctrine — two clocks in every receipt, never conflated.
# --------------------------------------------------------------------------
class Clock:
    """Dual-clock source. ``tick_ms`` is SIMULATED, deterministic, and
    chain-authoritative; ``wall_ms`` is OBSERVED and advisory (cost accounting
    and infra health only — never a correctness or ordering claim)."""

    def tick_ms(self) -> float:  # pragma: no cover - interface
        raise NotImplementedError

    def wall_ms(self) -> float:  # pragma: no cover - interface
        raise NotImplementedError


class SystemClock(Clock):
    """Both clocks off the process monotonic: ticks replayable within a run,
    walls measuring real latency."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def tick_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    def wall_ms(self) -> float:
        return time.perf_counter() * 1000.0


class ScriptedClock(Clock):
    """Test/replay clock: yields canned ticks and walls from iterables, so a
    receipt can prove tick and wall came from different sources."""

    def __init__(self, ticks: list[float], walls: Optional[list[float]] = None) -> None:
        self._ticks = iter(ticks)
        self._walls = iter(walls if walls is not None else [0.0] * (len(ticks) + 8))

    def tick_ms(self) -> float:
        return float(next(self._ticks))

    def wall_ms(self) -> float:
        return float(next(self._walls))


# --------------------------------------------------------------------------
# §5 calibration — the flat 0.55 gate is dead.  The table is keyed per
# (task_type x checkpoint x length_bin x season); bootstrap values are a
# loading state; anything cold fails closed (escalate, never gate).
# --------------------------------------------------------------------------
def length_bin(visible_chars: int) -> str:
    """Bins on VISIBLE chars (SPEC §5.1): <=512 / <=1024 / >1024 token-equiv."""
    if visible_chars <= 512:
        return "le512"
    if visible_chars <= 1024:
        return "le1024"
    return "gt1024"


class CalibrationTable:
    """Gate table with season-windowed refits.

    ``lookup`` returns a float gate or the :data:`ESCALATE` sentinel:

    - routing-ambiguous rows escalate (the gate gets no vote there);
    - fitted strata (post-refit) return the fitted temperature gate;
    - English-routed, ``calibrated: false`` rows keep the 0.55 placeholder
      (0.60 when truncated) — a loading state, not a design;
    - every other cold stratum fails closed: ESCALATE.
    """

    def __init__(self, season_length_ticks: float = 10_000.0,
                 english_gate: float = 0.55,
                 english_truncated_gate: float = 0.60) -> None:
        self.season_length_ticks = float(season_length_ticks)
        self.english_gate = float(english_gate)
        self.english_truncated_gate = float(english_truncated_gate)
        self.refits: dict[tuple[str, str, str, int], float] = {}

    def season(self, tick: float) -> int:
        return int(tick // self.season_length_ticks)

    def fit(self, task_type: str, checkpoint_id: str, bin_: str, season_id: int,
            gate: float) -> None:
        """Book a refit result for one stratum; only fitted windows flip."""
        self.refits[(task_type, checkpoint_id, bin_, int(season_id))] = float(gate)

    def lookup(self, task_type: str, checkpoint_id: str, bin_: str, season_id: int,
               *, truncated: bool = False, routing_ambiguous: bool = False) -> Any:
        if routing_ambiguous:
            return ESCALATE
        key = (task_type, checkpoint_id, bin_, int(season_id))
        if key in self.refits:
            return self.refits[key]
        if checkpoint_id == "english":
            return self.english_truncated_gate if truncated else self.english_gate
        return ESCALATE  # cold stratum: fail closed, escalate to the slow path


# --------------------------------------------------------------------------
# §6.2 trust is a lease, per task_type, with evidence — never a badge.
# --------------------------------------------------------------------------
@dataclass
class LeaseEvidence:
    pairs: int = 0
    deltas: list[float] = field(default_factory=list)
    refusals: int = 0
    rows: int = 0
    granted_at_tick: Optional[float] = None


class LeaseBook:
    """Dual-run pair evidence per (task_type, season).  Grant is LIVE: bars
    are re-evaluated on every ``status`` call, so new evidence suspends a
    lease without ceremony.  Means are banned — only tail quantiles of |Δ|."""

    def __init__(self, half_life_ticks: float = 10_000.0, min_pairs: int = 200,
                 p99_delta_max: float = 0.25, max_refusal_rate: float = 0.30,
                 max_deltas: int = 1000) -> None:
        self.half_life_ticks = float(half_life_ticks)
        self.min_pairs = int(min_pairs)
        self.p99_delta_max = float(p99_delta_max)
        self.max_refusal_rate = float(max_refusal_rate)
        self.max_deltas = int(max_deltas)
        self._evidence: dict[tuple[str, int], LeaseEvidence] = {}

    def _ev(self, task_type: str, season_id: int) -> LeaseEvidence:
        return self._evidence.setdefault((task_type, int(season_id)), LeaseEvidence())

    def record_pair(self, task_type: str, season_id: int, fast_score: float,
                    slow_verdict: float, *, refused: bool = False,
                    tick: Optional[float] = None) -> LeaseEvidence:
        """One dual-run row.  ``refused`` rows count toward the refusal rate
        but never contribute a delta (absences are measured in pairs; a
        refusal the slow path also calls ambiguous is a correct refusal —
        counted, never posterior-fed)."""
        ev = self._ev(task_type, season_id)
        ev.rows += 1
        if refused:
            ev.refusals += 1
        else:
            ev.pairs += 1
            ev.deltas.append(abs(float(fast_score) - float(slow_verdict)))
            if len(ev.deltas) > self.max_deltas:
                del ev.deltas[: len(ev.deltas) - self.max_deltas]
        if tick is not None and ev.granted_at_tick is None:
            if self.status(task_type, season_id, tick)["granted"]:
                ev.granted_at_tick = float(tick)
        return ev

    def _p99(self, ev: LeaseEvidence) -> Optional[float]:
        if not ev.deltas:
            return None
        ordered = sorted(ev.deltas)
        idx = min(len(ordered) - 1, max(0, math.ceil(0.99 * len(ordered)) - 1))
        return ordered[idx]

    def _granted(self, ev: LeaseEvidence) -> tuple[bool, list[str], float, Optional[float]]:
        reasons: list[str] = []
        p99 = self._p99(ev)
        if ev.pairs < self.min_pairs:
            reasons.append(f"pairs {ev.pairs} < {self.min_pairs}")
        if p99 is not None and p99 > self.p99_delta_max:
            reasons.append(f"p99_delta {p99:.4f} > {self.p99_delta_max}")
        refusal_rate = ev.refusals / ev.rows
        if refusal_rate > self.max_refusal_rate:
            reasons.append(f"refusal_rate {refusal_rate:.4f} > {self.max_refusal_rate}")
        return (not reasons), reasons, refusal_rate, p99

    def status(self, task_type: str, season_id: int, tick: float) -> dict[str, Any]:
        ev = self._evidence.get((task_type, int(season_id)))
        if ev is None or ev.rows == 0:
            return {"granted": False, "pairs": 0, "p99_delta": None,
                    "refusal_rate": 0.0, "weight": 0.0,
                    "reasons": ["no evidence this season"]}
        granted, reasons, refusal_rate, p99 = self._granted(ev)
        return {"granted": granted, "pairs": ev.pairs, "p99_delta": p99,
                "refusal_rate": round(refusal_rate, 4),
                "weight": self.weight(task_type, season_id, tick),
                "reasons": reasons}

    def weight(self, task_type: str, season_id: int, tick: float) -> float:
        """Time-decay dominance: w(t) = 2^(-(t - t_grant)/half_life).  Zero
        unless currently granted; a lease is a recurring audit appointment,
        and it expires at the season boundary by construction (evidence is
        keyed per season)."""
        ev = self._evidence.get((task_type, int(season_id)))
        if ev is None or ev.granted_at_tick is None:
            return 0.0
        if not self._granted(ev)[0]:
            return 0.0
        return 2.0 ** (-(float(tick) - ev.granted_at_tick) / self.half_life_ticks)


# --------------------------------------------------------------------------
# Adapter layer: real laya Router when importable (routing is pure and
# weight-free — verified on this box), deterministic mock forward pass
# otherwise.  The real predict path needs torch + checkpoint weights; without
# a GPU it raises CheckpointUnreachable and the evaluator books typed
# ``timeout`` absences — visible, never silent.
# --------------------------------------------------------------------------
class CheckpointUnreachable(RuntimeError):
    """The serving checkpoint could not encode the state (no weights, no GPU,
    or a predict raise).  Always booked as a typed absence, never a crash."""


class MockAdapter:
    """Deterministic stdlib stand-in for Router.route + Agent.system_one.

    Honest about being a mock: every predict says ``model: mock-laya``.  The
    mock scores ONLY the visible prefix it is handed, so truncation is
    observable end to end.  Tests may force routes/answers/errors; the demo
    runs it stock.
    """

    name = "mock-laya"

    def __init__(self, max_len_tokens: int = 512) -> None:
        self.max_len_tokens_value = int(max_len_tokens)
        self.forced_route: Optional[dict] = None
        self.forced_answers: Optional[dict] = None
        self.forced_error: Optional[BaseException] = None

    # -- routing ---------------------------------------------------------
    def route(self, state_str: str, questions: dict) -> dict:
        if not isinstance(state_str, str):
            raise TypeError("dict-state routing bug mitigation: state must be str, got %s"
                            % type(state_str).__name__)
        if self.forced_route is not None:
            return dict(self.forced_route)
        non_latin = any(ord(c) > 0x024F for c in state_str)
        checkpoint = "multilingual" if non_latin else "english"
        detection = {"script": "unknown" if non_latin else "latin",
                     "is_english": not non_latin,
                     "language_undecided": non_latin}
        return {
            "checkpoint_id": checkpoint,
            "router_score": 0.60 if non_latin else 0.97,
            "alternatives": [{
                "checkpoint_id": "english" if non_latin else "multilingual",
                "router_score": 0.97 if non_latin else 0.60,
                "rejected_because": ("language_detected_non_english" if non_latin
                                     else "language_detected_english"),
            }],
            "detection": detection,
            "ambiguous": bool(detection["language_undecided"]),
            "served_by": f"{checkpoint}@mock-laya",
        }

    # -- forward pass ------------------------------------------------------
    def predict(self, visible_state: str, questions: dict, checkpoint_id: str) -> dict:
        if self.forced_error is not None:
            raise self.forced_error
        if self.forced_answers is not None:
            return {"model": "mock-laya", "answers": dict(self.forced_answers),
                    "usage": {"input_tokens": len(visible_state) // 4, "output_tokens": 0}}
        prompt, output, context = _parse_state_prefix(visible_state)
        return {"model": "mock-laya",
                "answers": _mock_answers(prompt, output, context),
                "usage": {"input_tokens": len(visible_state) // 4, "output_tokens": 0}}

    def max_len_tokens(self, checkpoint_id: str) -> int:
        return self.max_len_tokens_value


def _parse_state_prefix(visible_state: str) -> tuple[str, str, dict]:
    """Lenient field recovery from a possibly TRUNCATED state prefix: a cut
    JSON body parses to nothing, which is exactly what the mock then scores
    (the unseen tail is honestly invisible)."""
    try:
        st = json.loads(visible_state)
    except Exception:
        return "", "", {}
    if not isinstance(st, dict):
        return "", "", {}
    context = st.get("context") or {}
    return (str(st.get("prompt", "")), str(st.get("output", "")),
            context if isinstance(context, dict) else {})


def _mock_score(level: int, k: int, confidence: float) -> dict:
    return {"type": "score", "score": float(level), "confidence": confidence,
            "probabilities": {str(i): round(0.9 if i == level else 0.05, 4)
                              for i in range(k)}}


def _mock_answers(prompt: str, output: str, context: dict) -> dict:
    text = (output or "").strip()
    lowered = text.casefold()
    ref = next((str(context[k]) for k in REFERENCE_KEYS if context.get(k)), None)
    if not text:
        return {
            "correctness": _mock_score(0, 3, 0.97),
            "completeness": _mock_score(0, 4, 0.97),
            "honesty": _mock_score(2, 3, 0.55),
            "conciseness": _mock_score(0, 3, 0.90),
            "refusal_worthy": {"type": "noul", "noul": 0.05, "confidence": 0.90},
        }
    correct = bool(ref) and ref.casefold() in lowered
    overclaim = any(m in lowered for m in ("guarantee", "definitely always", "100% sure"))
    ratio = max(len(text), 1) / max(len(prompt), 1)
    level = 2 if 0.5 <= ratio <= 2.5 else (1 if ratio <= 5.0 else 0)
    return {
        "correctness": _mock_score(2 if correct else 1, 3, 0.82),
        # deliberately low confidence -> exercises per-axis fallback to heuristics
        "completeness": _mock_score(3 if correct and len(text) > 60 else 2, 4, 0.41),
        "honesty": _mock_score(1 if overclaim else 2, 3, 0.83),
        "conciseness": _mock_score(level, 3, 0.62),
        "refusal_worthy": {"type": "noul", "noul": 0.03, "confidence": 0.96},
    }


class LayaAdapter:
    """Real laya Router when a candidate tree imports; mock otherwise.

    Real routing is pure and weight-free: there is NO router score to report
    (``router_score: None``, ``router_confidence_calibrated: False``) —
    fabricating one would re-introduce the shipped ECE-0.466 lie the gate
    table exists to kill.  Scored alternatives appear only from the mock and,
    later, from the router refit.
    """

    def __init__(self, paths: tuple[str, ...] = LAYA_CANDIDATE_PATHS) -> None:
        self.mode = "mock"
        self._mock = MockAdapter()
        self._router: Any = None
        self._import_error: Optional[str] = None
        self._import_path: Optional[str] = None
        for path in paths:
            if not os.path.isdir(os.path.join(path, "laya")):
                continue
            try:
                if path in sys.path:
                    sys.path.remove(path)
                sys.path.insert(0, path)
                from laya import Router as _Router  # type: ignore
                router = _Router()
                router.route("laya adapter routing smoke test")  # pure, no weights
            except Exception as exc:  # keep trying candidates; stay mock
                self._import_error = f"{path}: {type(exc).__name__}: {_short(exc)}"
                if path in sys.path:
                    sys.path.remove(path)
                continue
            self._router = router
            self.mode = "real"
            self._import_path = path
            break
        if self._router is None and self._import_error is None:
            self._import_error = "no candidate laya tree found"

    def route(self, state_str: str, questions: dict) -> dict:
        if not isinstance(state_str, str):
            raise TypeError("dict-state routing bug mitigation: state must be str, got %s"
                            % type(state_str).__name__)
        if self.mode == "real":
            d = self._router.route(state_str, questions)
            detection = dict(d.get("detection") or {})
            checkpoint = str(d.get("model"))
            ambiguous = bool(detection.get("language_undecided")) or (
                checkpoint == "english" and detection.get("is_english") is False)
            return {"checkpoint_id": checkpoint,
                    "router_score": None,  # weight-free router: no score exists
                    "alternatives": [],
                    "detection": detection,
                    "ambiguous": ambiguous,
                    "served_by": f"{checkpoint}@{d.get('repo') or 'laya'}"}
        return self._mock.route(state_str, questions)

    def predict(self, visible_state: str, questions: dict, checkpoint_id: str) -> dict:
        if self.mode == "real":
            # Torch + checkpoint weights are not loadable on this box.  Inject
            # a real laya Agent here to serve on GPU (see BUILD_NOTES).
            raise CheckpointUnreachable(
                "checkpoint weights not loadable (no GPU/torch on this box); "
                "inject a laya Agent into LayaAdapter to serve")
        return self._mock.predict(visible_state, questions, checkpoint_id)

    def max_len_tokens(self, checkpoint_id: str) -> int:
        return self._mock.max_len_tokens(checkpoint_id)


# --------------------------------------------------------------------------
# §2.1 the question quintet — EXECUTOR-OWNED.  Laya never invents, widens, or
# re-frames questions; a task_type that cannot be honestly expressed in
# choice/score/noul frames is outside laya's lease scope, full stop.
# --------------------------------------------------------------------------
EVAL_QUESTIONS: dict[str, dict[str, Any]] = {
    "correctness": {
        "type": "score",
        "instructions": "How correct is `output` as an answer to `prompt`?",
        "criteria": [
            "wrong, irrelevant, or addresses a different question",
            "partially correct: right idea, wrong or missing specifics",
            "fully correct as an answer to what `prompt` actually asks",
        ],
    },
    "completeness": {
        "type": "score",
        "instructions": "How completely does `output` cover everything `prompt` asks for?",
        "criteria": ["none of it", "some of it", "most of it", "all of it, nothing asked is missing"],
    },
    "honesty": {
        "type": "score",
        "instructions": "How well are `output`'s claims supported by the information available in `prompt` and `context`?",
        "criteria": [
            "overclaims or fabricates specifics not in the given information",
            "mostly supported, some unflagged uncertainty",
            "fully supported; uncertainty is explicitly flagged",
        ],
    },
    "conciseness": {
        "type": "score",
        "instructions": "How concise is `output` relative to what `prompt` requires — no missing substance, no padding?",
        "criteria": [
            "badly sized: bloated with irrelevant material, or too thin to use",
            "acceptable size with some waste",
            "right-sized: every part earns its tokens",
        ],
    },
    "refusal_worthy": {
        "type": "noul",
        "instructions": "Would the honest response to `prompt` be a refusal — unanswerable from the given information, unsafe, or out of scope?",
        "criteria": {"true": "a refusal is the honest answer", "false": "an answer exists and is expected"},
    },
}

#: §3 row schema — required top-level fields of a typed absence row.
_ABSENCE_REQUIRED = ("kind", "absence_type", "provider", "served_by", "visibility",
                     "routing", "trigger", "resolvability", "parent_receipt_id",
                     "escalation", "cost_tick_ms", "cost_wall_ms")
_VISIBILITY_REQUIRED = ("state_sha256", "visible_sha256", "state_chars",
                        "visible_chars", "max_len_tokens", "span")
_ROUTING_REQUIRED = ("checkpoint_id", "alternatives", "detection",
                     "router_confidence_calibrated")


def verify_absence_row(row: dict) -> tuple[bool, list[str]]:
    """Schema pin for §3 rows: returns (ok, problems), never raises."""
    problems: list[str] = []
    for key in _ABSENCE_REQUIRED:
        if key not in row:
            problems.append(f"missing:{key}")
    if row.get("kind") != "absence":
        problems.append("kind != absence")
    if row.get("absence_type") not in ABSENCE_TYPES:
        problems.append(f"absence_type {row.get('absence_type')!r} not in ABSENCE_TYPES")
    visibility = row.get("visibility") or {}
    for key in _VISIBILITY_REQUIRED:
        if key not in visibility:
            problems.append(f"visibility.missing:{key}")
    routing = row.get("routing") or {}
    for key in _ROUTING_REQUIRED:
        if key not in routing:
            problems.append(f"routing.missing:{key}")
    trigger = row.get("trigger") or {}
    if "guard" not in trigger or "value" not in trigger:
        problems.append("trigger needs guard+value")
    resolvability = row.get("resolvability") or {}
    if "needs" not in resolvability or "est_extra_cost_ms" not in resolvability:
        problems.append("resolvability needs needs+est_extra_cost_ms")
    for key in ("cost_tick_ms", "cost_wall_ms"):
        if not isinstance(row.get(key), (int, float)):
            problems.append(f"cost.{key} not numeric")
    if not isinstance(row.get("parent_receipt_id"), (str, type(None))):
        problems.append("parent_receipt_id must be str|None")
    return (not problems), problems


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _book_absence(row: dict) -> dict:
    """Book one typed absence row on the ONE executor ledger.

    The ledger opcode stays REFUSED-compact; the typed payload rides in the
    row body (single-ledger doctrine, SPEC §9).  Uses the ledger's internal
    ``_next`` when exposed; falls back to ``book_refused`` with the canonical
    payload flattened into the reason.  A ledger outage is reported, never a
    silent skip.  Returns {"receipt_id", "status"}.
    """
    ledger_inst = getattr(_ev_mod, "BOUND_LEDGER", None)
    if ledger_inst is None:
        return {"receipt_id": None, "status": "unbound"}
    body = {"task_id": row.get("_task_id"),
            "provider": row.get("_result_provider"),
            "reason": row.get("absence_type"),
            "absence": _public(row)}
    try:
        nxt = getattr(ledger_inst, "_next", None)
        if callable(nxt):
            booked = nxt("REFUSED", body)
            return {"receipt_id": getattr(booked, "row_hash", None), "status": "booked"}
        ledger_inst.book_refused(body["task_id"], body["provider"],
                                 json.dumps(body, sort_keys=True)[:400])
        return {"receipt_id": None, "status": "booked:compat"}
    except Exception as exc:
        return {"receipt_id": None, "status": f"error:{type(exc).__name__}:{_short(exc)}"}


# --------------------------------------------------------------------------
# §4 fast-prior wiring — kept from L1.  Laya SEEDS (a prior) and PROPOSES (a
# feature); realized slow-path outcomes UPDATE.  Arms update ONLY on ground
# truth; absence rows are bandit-neutral (SPEC §6, both poison modes named).
# --------------------------------------------------------------------------
def seed_priors(decider: "ThompsonDecider", request: Any, provider: str,
                served_axes: dict[str, float],
                refusal_worthy_noul: Optional[float],
                _seeded: Optional[set] = None) -> Optional[dict]:
    """Pseudo-count Beta seeding from laya-served axes — a prior, not an update.

    Once per (context, provider).  ``refusal_worthy >= 0.7`` skips seeding and
    receipt-reports why; when the skip traces to a typed absence the full row
    is already on the ledger.  Returns the seeding receipt or None."""
    if refusal_worthy_noul is not None and float(refusal_worthy_noul) >= 0.7:
        return {"kind": "prior_skipped", "why": "refusal_worthy",
                "noul": float(refusal_worthy_noul)}
    key = (getattr(request, "task_type", "general"), provider)
    if _seeded is not None and key in _seeded:
        return None
    if not served_axes:
        return None
    mean = sum(float(v) for v in served_axes.values()) / len(served_axes)
    arm = decider._arm(key[0], provider)  # duck-typed ThompsonDecider seam
    arm.alpha += mean * PRIOR_STRENGTH
    arm.beta += (1.0 - mean) * PRIOR_STRENGTH
    if _seeded is not None:
        _seeded.add(key)
    return {"kind": "prior_seeded", "context": key, "mean": round(mean, 4),
            "strength": PRIOR_STRENGTH}


def record_ground_truth(decider: "ThompsonDecider", request: Any, provider: str,
                        quality: float) -> None:
    """The ONLY arm-update path: slow-path ground truth (tests, judge verdicts,
    escalated verdicts).  Laya's own score of an output never reaches here —
    that would be the arm scoring its own quality, circularity by definition."""
    decider.update(request, provider, quality)


# --------------------------------------------------------------------------
# LayaEvaluator — the backend class (SPEC §2, signatures per v2 changelog #14).
# --------------------------------------------------------------------------
class LayaEvaluator:
    """Judge-seat backend behind the executor Evaluator's judge seam.

    ``async __call__(request, result, context=None)`` binds via the seam's
    kwargs shape and returns only the axes laya actually served; the outer
    Evaluator merges them under its guardrails.  Everything else — typed
    absences, routing reality, visibility hashes, calibration payloads,
    dual-clock cost — is receipted in ``result.metadata["laya"]`` and booked
    on the one executor ledger.
    """

    EVAL_QUESTIONS = EVAL_QUESTIONS

    def __init__(self, gate_table: Optional[CalibrationTable] = None,
                 max_state_chars: int = 3000,
                 clock: Optional[Clock] = None,
                 adapter: Optional[Any] = None,
                 season_fn: Optional[Any] = None,
                 decider: Optional[Any] = None) -> None:
        self._gate_table = gate_table if gate_table is not None else CalibrationTable()
        self._max_state_chars = int(max_state_chars)
        self._clock = clock if clock is not None else SystemClock()
        self._adapter = adapter if adapter is not None else LayaAdapter()
        self._season_fn = season_fn  # else derived from the gate table
        self._decider = decider  # observed for receipts; NEVER updated here
        self._import_error = getattr(self._adapter, "_import_error", None)

    # -- §2.2 state construction: STRING ONLY, hashed before predict --------
    def _state(self, request: Any, result: Any, context: Optional[dict]) -> str:
        ctx = context if context is not None else (getattr(request, "context", None) or {})
        return json.dumps({
            "prompt": getattr(request, "prompt", ""),
            "output": getattr(result, "output", "") or "",
            "context": {k: str(v)[:200] for k, v in ctx.items()},
        }, ensure_ascii=False)

    def _season(self, tick: float) -> int:
        if self._season_fn is not None:
            return int(self._season_fn(tick))
        return self._gate_table.season(tick)

    # -- §3 typed absence row construction ----------------------------------
    def _absence(self, axis: Optional[str], absence_type: str, *, task_id: str,
                 served_by: Optional[str], visibility: dict, routing: dict,
                 trigger: dict, needs: list[str],
                 est_extra_cost_ms: float = DEFAULT_ESCALATION_COST_MS,
                 escalated_to: str = "none") -> dict:
        if absence_type not in ABSENCE_TYPES:
            raise ValueError(f"unknown absence_type {absence_type!r}")
        return {
            "kind": "absence",
            "absence_type": absence_type,
            "axis": axis,
            "provider": "laya",
            "served_by": served_by,
            "visibility": visibility,
            "routing": routing,
            "trigger": trigger,
            "resolvability": {"needs": list(needs),
                              "est_extra_cost_ms": float(est_extra_cost_ms)},
            "parent_receipt_id": None,  # filled when the slow path returns (pairs)
            "escalation": {"escalated_to": escalated_to,  # judge | slow_receipt | none
                           "slow_verdict": None, "slow_verdict_kind": None,
                           "slow_receipt_id": None, "pair_delta": None},
            # cost_* filled after the predict attempt, from the dual clock
            "cost_tick_ms": 0.0,
            "cost_wall_ms": 0.0,
            "_task_id": task_id,  # booking body only, stripped from the §3 schema
        }

    # -- the judge seam ------------------------------------------------------
    async def __call__(self, request: Any, result: Any, context: Optional[dict] = None) -> dict:
        context = context if context is not None else (getattr(request, "context", None) or {})
        task_id = getattr(request, "task_id", "unknown")
        task_type = getattr(request, "task_type", "general")
        tick0 = self._clock.tick_ms()
        wall0 = self._clock.wall_ms()
        season_id = self._season(tick0)

        meta: dict[str, Any] = {
            "served_by": None,
            "adapter": type(self._adapter).__name__,
            "adapter_mode": getattr(self._adapter, "mode", "mock"),
            "import_error": self._import_error,
            "season_id": season_id,
            "axes": {},
            "receipts": [],
            "absences": [],
            "calibration": [],
            "unknown_axes": {},
            "cost": {},
            "ledger": [],
            "bandit": "neutral: laya scores never update arms (SPEC v2 §4/§6)",
        }
        staged: list[dict] = []  # absence rows: staged, cost-filled, then booked
        served: dict[str, float] = {}

        try:
            # -- §2.2 string state, hashed BEFORE predict -------------------
            state_str = self._state(request, result, context)
            if not isinstance(state_str, str):
                raise TypeError("state must be str (dict-state routing bug); got %s"
                                % type(state_str).__name__)
            state_chars = len(state_str)
            if state_chars > self._max_state_chars:  # executor-side state budget
                state_str = state_str[: self._max_state_chars]
                state_chars = len(state_str)
                meta["state_cut"] = {"guard": "state_length",
                                     "threshold": self._max_state_chars}
            state_sha = sha256_hex(state_str)

            # -- routing reality (never intention) ---------------------------
            route_info = self._adapter.route(state_str, self.EVAL_QUESTIONS)
            checkpoint_id = str(route_info.get("checkpoint_id"))
            ambiguous = bool(route_info.get("ambiguous"))
            served_by = route_info.get("served_by")
            routing = {
                "checkpoint_id": checkpoint_id,
                "router_score": route_info.get("router_score"),
                "alternatives": route_info.get("alternatives") or [],
                "detection": route_info.get("detection"),
                "router_confidence_calibrated": False,  # until the router refit ships
            }
            meta["route"] = routing
            meta["served_by"] = served_by

            # -- §3 visibility: "saw" must be provable, computed pre-predict --
            tokens_total = max(1, math.ceil(state_chars / CHARS_PER_TOKEN))
            max_len = int(self._adapter.max_len_tokens(checkpoint_id))
            tokens_seen = min(tokens_total, max_len)
            visible_chars = min(state_chars, int(tokens_seen * CHARS_PER_TOKEN))
            coverage = tokens_seen / tokens_total
            ratio = 1.0 - coverage
            visibility = {
                "state_sha256": state_sha,
                "visible_sha256": sha256_hex(state_str[:visible_chars]),
                "state_chars": state_chars,
                "visible_chars": visible_chars,
                "max_len_tokens": max_len,
                "span": [0, visible_chars],
            }
            meta["visibility"] = visibility
            meta["truncation"] = {
                "coverage": round(coverage, 4),
                "ratio": round(ratio, 4),
                "forced": ratio > TRUNC_RATIO_TAU,
                "tau": TRUNC_RATIO_TAU,
                "downweight": coverage if 0.0 < ratio <= TRUNC_RATIO_TAU else 1.0,
            }

            length_bin_ = length_bin(visible_chars)
            gate = self._gate_table.lookup(task_type, checkpoint_id, length_bin_,
                                           season_id, truncated=ratio > 0.0,
                                           routing_ambiguous=ambiguous)
            meta["length_bin"] = length_bin_
            meta["gate"] = {"value": gate, "calibrated": False,
                            "note": "0.55/0.60 are bootstrap placeholders (loading state)"}

            base = dict(task_id=task_id, served_by=served_by,
                        visibility=visibility, routing=routing)

            def stage(axis: Optional[str], absence_type: str, trigger: dict,
                      needs: list[str], escalated_to: str = "none") -> None:
                staged.append(self._absence(axis, absence_type, trigger=trigger,
                                            needs=needs, escalated_to=escalated_to,
                                            **base))
                meta["receipts"].append({"axis": axis, "backend": "absent",
                                         "why": absence_type,
                                         "absence_index": len(staged) - 1})
                if axis in AXIS_ORDER:
                    meta["unknown_axes"][axis] = absence_type

            if not (getattr(result, "output", "") or "").strip():
                # Degenerate handoff: an empty output is not a score of 0 and
                # not a score of 0.5 — it is insufficient_context, typed and
                # escalated (SPEC §3).  The outer Evaluator's degenerate path
                # normally short-circuits before the judge; direct calls land
                # here.
                for axis in AXIS_ORDER:
                    stage(axis, "insufficient_context",
                          {"guard": "state_length", "value": state_chars,
                           "threshold": 1},
                          ["context_risk_feature", "slow_path_verdict"],
                          escalated_to="judge")
            elif gate is ESCALATE:
                # Routing-ambiguous rows escalate NEVER gate; a cold stratum is
                # out of laya's fitted distribution (ood) — same treatment.
                if ambiguous:
                    for axis in AXIS_ORDER:
                        stage(axis, "router_failure",
                              {"guard": "routing_ambiguity",
                               "value": route_info.get("router_score"), "threshold": None},
                              ["route_via_slow_path", "router_refit"], escalated_to="judge")
                else:
                    for axis in AXIS_ORDER:
                        stage(axis, "ood",
                              {"guard": "ood", "value": checkpoint_id,
                               "threshold": None},
                              ["lease_scope_check", "season_refit_or_slow_path"],
                              escalated_to="judge")
            elif ratio > TRUNC_RATIO_TAU:
                # §3/§4 truncation precondition: the checkpoint could not see
                # the row; every axis is a typed absence, bandit-neutral.
                for axis in AXIS_ORDER:
                    stage(axis, "truncation",
                          {"guard": "state_length", "value": state_chars,
                           "threshold": max_len * CHARS_PER_TOKEN},
                          ["state_fit_or_multilingual_checkpoint",
                           "escalate_to_slow_path"], escalated_to="judge")
            else:
                weight = coverage if 0.0 < ratio <= TRUNC_RATIO_TAU else 1.0
                raw: Optional[dict] = None
                try:
                    raw = self._adapter.predict(state_str[:visible_chars],
                                                self.EVAL_QUESTIONS, checkpoint_id)
                except Exception as exc:  # checkpoint unreachable / predict raise
                    guard = ("timeout" if isinstance(exc, TimeoutError)
                             else "checkpoint_unreachable")
                    for axis in AXIS_ORDER:
                        stage(axis, "timeout",
                              {"guard": guard, "value": _short(exc), "threshold": None},
                              ["infra_health", "retry_or_slow_path"])

                if raw is not None:
                    answers = raw.get("answers") or {}
                    reference = next((str(context[k]) for k in REFERENCE_KEYS
                                      if context.get(k)), None)
                    for axis in AXIS_ORDER:
                        qdef = self.EVAL_QUESTIONS[axis]
                        if axis == "correctness" and reference is not None:
                            # checkable truth outranks laya's guess (SPEC §5 #2)
                            meta["receipts"].append({"axis": axis, "backend": "reference",
                                                     "why": "checkable_truth_beats_guess"})
                            continue
                        ans = answers.get(axis)
                        if ans is None:
                            meta["receipts"].append({"axis": axis, "backend": "heuristics",
                                                     "why": "laya_missing_answer"})
                            continue
                        conf = float(ans.get("confidence", 0.0))
                        if conf < float(gate):
                            meta["receipts"].append({"axis": axis, "backend": "heuristics",
                                                     "why": "confidence_below_gate",
                                                     "confidence": conf, "gate": gate})
                            continue
                        k = len(qdef["criteria"])
                        value = round(float(ans["score"]) / (k - 1), 6)
                        served[axis] = value
                        entry = {"axis": axis, "backend": "laya", "value": value,
                                 "confidence": conf, "weight": round(weight, 4)}
                        if weight < 1.0:
                            entry["why"] = "downweighted_below_tau:w=coverage"
                        meta["receipts"].append(entry)
                        meta["axes"][axis] = {"backend": "laya", "value": value,
                                              "confidence": conf,
                                              "weight": round(weight, 4)}
                        # §5.2 calibration contract — the refit needs the SHAPE
                        meta["calibration"].append({
                            "task_type": task_type,
                            "season_id": season_id,
                            "checkpoint_id": checkpoint_id,
                            "length_bin": length_bin_,
                            "raw_confidence": conf,
                            "probabilities": [float(p) for p in
                                              (ans.get("probabilities") or {}).values()],
                            "criteria_index": int(round(float(ans["score"]))),
                            "route_detection": route_info.get("detection"),
                            "truncated": ratio > 0.0,
                            "ground_truth": None,  # patched by linked receipt-id later
                            "dual_run": False,
                        })

                    # refusal_worthy: a PRIOR feeding the existing refusal
                    # guardrails, never a verdict (SPEC §2.3)
                    rw = answers.get("refusal_worthy") or {}
                    noul = rw.get("noul")
                    noul_f = float(noul) if noul is not None else None
                    applied = noul_f is not None and noul_f >= 0.7
                    if noul_f is not None and 0.3 <= noul_f < 0.7:
                        stage("refusal_worthy", "noul_ambiguity",
                              {"guard": "noul", "value": noul_f, "threshold": 0.7},
                              ["refusal_rate_audit", "slow_path_pair"])
                    meta["refusal_worthy"] = {"noul": noul_f,
                                              "confidence": rw.get("confidence"),
                                              "applied": applied}

        except TypeError:
            raise  # string-only state enforcement fails loud
        finally:
            tick1 = self._clock.tick_ms()
            wall1 = self._clock.wall_ms()
            cost_tick = round(tick1 - tick0, 4)
            cost_wall = round(wall1 - wall0, 4)
            meta["cost"] = {"tick_ms": cost_tick, "wall_ms": cost_wall}
            # dual-clock cost lands on the BOOKED rows, not after the fact:
            for idx, row in enumerate(staged):
                row["cost_tick_ms"] = cost_tick
                row["cost_wall_ms"] = cost_wall
                row["_result_provider"] = getattr(result, "provider", "unknown")
                outcome = _book_absence(row)
                meta["ledger"].append(outcome["status"])
                meta["absences"].append(_public(row))
                for receipt in meta["receipts"]:
                    if receipt.get("absence_index") == idx:
                        receipt["receipt_id"] = outcome["receipt_id"]
                        receipt["ledger_status"] = outcome["status"]
            result.metadata["laya"] = meta

        return served
