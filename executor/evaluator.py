"""Quality scoring for task results.

Deterministic stdlib heuristics always run and act as floor/ceiling
guardrails. An injected ``judge`` callable (typically an LLM) may override
individual axes, but never past those rails; a judge that raises is a
*visible* refusal -- heuristics stand and the reason lands in
``result.metadata["judge"]``. Every score path books a receipt through
``ledger.book_effect``.
"""

import dataclasses
import inspect
import math
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

try:  # in-package import, with a flat fallback for script-style harnesses
    from . import ledger
    from .ledger import TaskRequest, TaskResult
except ImportError:  # pragma: no cover
    import ledger  # type: ignore
    from ledger import TaskRequest, TaskResult  # type: ignore

#: Axis weights for :meth:`QualityScore.compute_overall` (COMET-composite style).
WEIGHTS: dict[str, float] = {
    "correctness": 0.35,
    "completeness": 0.25,
    "honesty": 0.20,
    "conciseness": 0.20,
}

#: Acceptable ``len(output) / expected_length`` ratio; outside this band the
#: flat :data:`DRIFT_PENALTY` is subtracted from the composite.
DRIFT_BAND: tuple[float, float] = (0.2, 5.0)
DRIFT_PENALTY: float = 0.3

#: Completeness ceiling for a refusal: honest, but not an answer.
REFUSAL_COMPLETENESS: float = 0.3

#: Case-folded substrings marking an honest refusal rather than an answer.
REFUSAL_MARKERS: tuple[str, ...] = ("i cannot", "unable to", "refusal")

#: ``request.context`` keys that may carry a reference answer for correctness.
REFERENCE_KEYS: tuple[str, ...] = ("expected", "expected_answer", "reference", "answer")


def _clamp01(value: float) -> float:
    """Clamp *value* into [0, 1]; scores never leave that range."""
    return max(0.0, min(1.0, float(value)))


def _short(reason: Any) -> str:
    """Flatten an exception/reason to one short metadata-safe line."""
    return str(reason).replace("\n", " ")[:160]


@dataclass
class QualityScore:
    """Four quality axes plus their weighted composite.

    Axes are floats in [0, 1], or ``None`` when the axis went unscored;
    :meth:`compute_overall` renormalizes over the axes that are present so a
    missing axis neither zeroes the composite nor counts as a zero.
    """

    correctness: Optional[float] = None
    completeness: Optional[float] = None
    honesty: Optional[float] = None
    conciseness: Optional[float] = None
    overall: float = 0.0

    def compute_overall(self) -> float:
        """Return (and store on ``self.overall``) the weighted composite.

        Weights come from :data:`WEIGHTS`; axes that are ``None`` are skipped
        and the remaining weights renormalized (COMET composite style). An
        all-``None`` score composites to 0.0.
        """
        total = weight = 0.0
        for axis, axis_weight in WEIGHTS.items():
            value = getattr(self, axis)
            if value is None:
                continue
            total += axis_weight * _clamp01(value)
            weight += axis_weight
        self.overall = round(total / weight, 6) if weight else 0.0
        return self.overall


def _accepts_positional(params: Any, count: int) -> bool:
    """Whether a signature can take *count* positional arguments."""
    values = list(params.values())
    positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    slots = sum(1 for p in values if p.kind in positional)
    varargs = any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in values)
    required_kw = any(p.kind is inspect.Parameter.KEYWORD_ONLY
                      and p.default is inspect.Parameter.empty for p in values)
    return (slots >= count or varargs) and not required_kw


def _call_flexibly(
    func: Callable[..., Any],
    payload: dict[str, Any],
    fallbacks: Sequence[tuple[Any, ...]] = (),
) -> tuple[Any, str]:
    """Call *func*, adapting to whatever signature it actually declares.

    Neither ``ledger.book_effect`` nor an injected judge has its signature
    pinned by the shared spec, so we bind by parameter name and pass only what
    matches; then try the positional *fallbacks*; then hand the whole payload
    dict to a single-parameter callable. Only signature-mismatch ``TypeError``
    advances to the next shape -- errors raised inside the callable propagate.
    Returns ``(value, shape)``; raises ``TypeError`` when nothing binds.
    """
    try:
        params: Any = inspect.signature(func).parameters
    except (TypeError, ValueError):
        params = None
    if params is not None:
        matched = {k: v for k, v in payload.items() if k in params}
        if matched:
            try:
                return func(**matched), "kwargs"
            except TypeError:
                pass
        for args in fallbacks:
            if _accepts_positional(params, len(args)):
                try:
                    return func(*args), f"positional:{len(args)}"
                except TypeError:
                    continue
        positional = [
            p
            for p in params.values()
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if len(positional) == 1 and _accepts_positional(params, 1):
            try:
                return func(payload), "payload"
            except TypeError:
                pass
    raise TypeError(
        f"no signature shape of {getattr(func, '__name__', func)!r} "
        f"accepts payload keys {sorted(payload)}"
    )


def _receipt_row(row_cls: Any, payload: dict[str, Any]) -> Any:
    """Best-effort ``ReceiptRow`` from the payload fields it actually declares."""
    fields = {f.name for f in dataclasses.fields(row_cls)}
    return row_cls(**{k: v for k, v in payload.items() if k in fields})


#: Bound ledger INSTANCE for booking. ``from . import ledger`` imports the
#: MODULE (unbound methods); real receipts need an instance. Orchestrator
#: binds one per run; unbound = visible refusal, never a silent skip.
BOUND_LEDGER: Any = None


def bind_ledger(inst: Any) -> None:
    """Wire the ledger instance receipts are booked into."""
    global BOUND_LEDGER
    BOUND_LEDGER = inst


def _book_effect(payload: dict[str, Any]) -> tuple[Any, str]:
    """Book *payload* through the bound ledger's ``book_effect``.

    First shape is the real quilt-executor contract:
    ``book_effect(request, chosen, quality, rivals)``. Further shapes are
    the lane's original runtime adaptations, kept as fallbacks.
    """
    ledger_inst = BOUND_LEDGER
    if ledger_inst is None:
        raise RuntimeError("no ledger bound: executor.evaluator.bind_ledger(inst)")
    book = getattr(ledger_inst, "book_effect", None)
    if book is None:
        raise RuntimeError("bound ledger exposes no book_effect")
    req = TaskRequest(
        task_id=payload["task_id"],
        prompt=payload["prompt"],
        task_type=payload.get("task_type", "general"),
    )
    chosen = TaskResult(
        output=payload.get("output", ""),
        provider=payload["provider"],
        latency_ms=payload.get("latency_ms", 0.0),
        cost_usd=payload.get("cost_usd", 0.0),
        error=payload.get("error"),
    )
    attempts: list[tuple[str, Callable[[], Any]]] = [("core4",
        lambda: book(req, chosen, payload.get("quality", 0.0), []))]
    row_cls = getattr(type(ledger_inst), "ReceiptRow", None) or getattr(ledger, "ReceiptRow", None)
    if row_cls is not None and dataclasses.is_dataclass(row_cls):
        attempts.append(("receipt_row", lambda: book(_receipt_row(row_cls, payload))))
    attempts.append(("kwargs", lambda: _call_flexibly(book, payload)[0]))
    attempts.append(("payload", lambda: book(payload)))
    rejected: list[str] = []
    for shape, thunk in attempts:
        try:
            return thunk(), shape
        except TypeError as exc:
            rejected.append(f"{shape}: {_short(exc)}")
    raise RuntimeError("ledger.book_effect rejected every call shape: " + " | ".join(rejected))


def _reference(request: TaskRequest) -> Optional[str]:
    """Reference answer supplied in ``request.context``, if any."""
    for key in REFERENCE_KEYS:
        value = request.context.get(key)
        if value:
            return str(value)
    return None


def _drift_ratio(request: TaskRequest, text: str) -> float:
    """``len(output) / expected_length`` for the length-drift heuristic.

    The expectation comes from ``context["expected_length"]`` /
    ``context["reference_length"]`` when supplied, else a coarse "answer about
    as long as the prompt" prior; it is floored at 1 so the ratio is finite.
    """
    context = request.context or {}
    raw = context.get("expected_length") or context.get("reference_length") or len(request.prompt)
    try:
        expected = max(1.0, float(raw))
    except (TypeError, ValueError):
        expected = float(max(1, len(request.prompt)))
    return max(len(text), 1) / expected


def _degenerate(note: str) -> tuple[QualityScore, list[str], dict[str, bool]]:
    """A result that cannot be scored at all: every axis 0, the judge moot."""
    return (
        QualityScore(0.0, 0.0, 0.0, 0.0),
        [note],
        {"degenerate": True, "refusal": False, "drift": False},
    )


def _heuristics(
    request: TaskRequest, result: TaskResult
) -> tuple[QualityScore, list[str], dict[str, bool]]:
    """Deterministic floor score; no model, no network, no discretion.

    Returns the heuristic axes, notes for the audit trail, and flags
    (``degenerate`` / ``refusal`` / ``drift``) the guardrails act on.
    """
    notes: list[str] = []
    text = result.output or ""
    if result.error:
        return _degenerate(f"provider_error:{_short(result.error)}")
    if not text.strip():
        # A silent failure is not an honest refusal: nothing scores above 0.
        return _degenerate("empty_output:all_axes_zero")

    score = QualityScore()
    lowered = text.casefold()
    marker = next((m for m in REFUSAL_MARKERS if m in lowered), None)
    if marker:
        # The point of the exercise: an honest refusal is high-honesty AND
        # low-completeness, not a zero.
        score.honesty = 1.0
        score.completeness = REFUSAL_COMPLETENESS
        notes.append(f"refusal:{marker!r}:honesty=1.0,completeness<={REFUSAL_COMPLETENESS}")
    else:
        score.honesty = 1.0  # presumed honest absent a refusal marker

    expected = _reference(request)
    if expected is not None:
        score.correctness = 1.0 if expected.casefold() in lowered else 0.0
        notes.append(f"correctness:reference_match={score.correctness}")
    else:
        score.correctness = 0.5  # no ground truth in context; neutral prior
        notes.append("correctness:prior=0.5")
    if score.completeness is None:
        score.completeness = 0.6
        notes.append("completeness:prior=0.6")

    low, high = DRIFT_BAND
    ratio = _drift_ratio(request, text)
    score.conciseness = max(0.0, 1.0 - abs(math.log(ratio)) / math.log(high))
    drift = ratio < low or ratio > high
    if drift:
        notes.append(f"length_drift:ratio={ratio:.2f}:penalty={DRIFT_PENALTY}")
    return score, notes, {"degenerate": False, "refusal": marker is not None, "drift": drift}


def _merge_judgement(
    score: QualityScore, response: Any, notes: list[str]
) -> tuple[bool, Optional[float]]:
    """Apply a judge *response* onto *score*; report an overall override, if any.

    Accepts a :class:`QualityScore`, a dict of axis names, or a bare number
    meaning "this overall". Raises ``ValueError`` on anything unusable (the
    caller turns that into a judge refusal). A literal ``0`` overall hint is
    indistinguishable from "no hint" and is treated as absent.
    """
    if isinstance(response, QualityScore):
        axes = {a: getattr(response, a) for a in WEIGHTS if getattr(response, a) is not None}
        hint: Any = response.overall
    elif isinstance(response, dict):
        axes = {a: response[a] for a in WEIGHTS if response.get(a) is not None}
        hint = response.get("overall")
    elif isinstance(response, (int, float)):
        axes, hint = {}, float(response)
    else:
        raise ValueError(f"unusable judge response: {type(response).__name__}")
    for axis, value in axes.items():
        clamped = _clamp01(value)
        notes.append(f"judge:{axis}={clamped:.4f}")
        setattr(score, axis, clamped)
    has_hint = isinstance(hint, (int, float)) and bool(hint)
    return has_hint, (_clamp01(hint) if has_hint else None)


def _apply_guardrails(score: QualityScore, flags: dict[str, bool], notes: list[str]) -> None:
    """Clamp judge-adjusted axes back onto the deterministic rails, in place."""
    for axis in WEIGHTS:
        value = getattr(score, axis)
        if value is None:
            continue
        clamped = _clamp01(value)
        if flags["refusal"] and axis == "honesty":
            clamped = max(clamped, 1.0)  # an honest refusal cannot be judged dishonest
        if flags["refusal"] and axis == "completeness":
            clamped = min(clamped, REFUSAL_COMPLETENESS)  # ...nor judged complete
        if clamped != value:
            notes.append(f"guardrail:{axis}:{round(value, 4)}->{round(clamped, 4)}")
        setattr(score, axis, clamped)


class Evaluator:
    """Scores a :class:`TaskResult` against its :class:`TaskRequest`.

    Heuristics always run; ``judge`` may refine any axis within the
    guardrails. Every path -- clean score, judge refusal, judge fallback,
    ledger outage -- is written into ``result.metadata`` and booked to the
    ledger, so nothing downstream has to trust a silent default.
    """

    async def evaluate(
        self,
        request: TaskRequest,
        result: TaskResult,
        judge: Optional[Callable[..., Any]] = None,
    ) -> QualityScore:
        """Score *result*, audit it into ``result.metadata``, book the receipt."""
        score, notes, flags = _heuristics(request, result)
        judge_mode = "unused"
        overall_hint: Optional[float] = None
        if judge is not None:
            if flags["degenerate"]:
                judge_mode = "skipped:degenerate_output"
                notes.append(f"judge:{judge_mode}")
            else:
                judge_mode, overall_hint = await self._consult(
                    judge, request, result, score, notes
                )
        _apply_guardrails(score, flags, notes)
        score.overall = score.compute_overall() if overall_hint is None else _clamp01(overall_hint)
        if flags["drift"]:
            score.overall = _clamp01(score.overall - DRIFT_PENALTY)
        self._audit(result, score, judge_mode, notes)
        self._book(request, result, score)
        return score

    async def _consult(
        self,
        judge: Callable[..., Any],
        request: TaskRequest,
        result: TaskResult,
        score: QualityScore,
        notes: list[str],
    ) -> tuple[str, Optional[float]]:
        """Run the injected judge and merge its axes into the heuristic score.

        Returns ``(mode, overall_hint)`` where ``mode`` is ``"used:<shape>"``
        or ``"refused:<reason>"``. A judge that raises is a refusal, not a
        crash: the heuristic axes stand and the reason stays visible.
        """
        payload = {
            "request": request,
            "result": result,
            "prompt": request.prompt,
            "output": result.output,
            "task_id": request.task_id,
            "task_type": request.task_type,
            "context": request.context,
            "provider": result.provider,
            "error": result.error,
        }
        try:
            outcome, shape = _call_flexibly(judge, payload, ((request, result), (result,)))
            if inspect.isawaitable(outcome):
                outcome = await outcome
            has_hint, hint = _merge_judgement(score, outcome, notes)
            return f"used:{shape}", hint if has_hint else None
        except Exception as exc:  # any judge failure is a refusal, never a crash
            mode = f"refused:{type(exc).__name__}:{_short(exc)}"
            notes.append(f"judge:{mode}")
            return mode, None

    @staticmethod
    def _audit(
        result: TaskResult, score: QualityScore, judge_mode: str, notes: list[str]
    ) -> None:
        """Write the scoring audit trail into ``result.metadata``."""
        result.metadata["evaluator"] = (
            "heuristics" if judge_mode == "unused" else f"heuristics+judge({judge_mode})"
        )
        result.metadata["judge"] = judge_mode
        result.metadata["score_overall"] = round(score.overall, 4)
        result.metadata["score_axes"] = {
            axis: (None if getattr(score, axis) is None else round(getattr(score, axis), 4))
            for axis in WEIGHTS
        }
        result.metadata["score_notes"] = list(notes)

    @staticmethod
    def _book(request: TaskRequest, result: TaskResult, score: QualityScore) -> None:
        """Book one receipt per score path; a ledger outage is visible, not fatal."""
        payload = {
            "task_id": request.task_id,
            "task_type": request.task_type,
            "prompt": request.prompt,
            "provider": result.provider,
            "latency_ms": result.latency_ms,
            "cost_usd": result.cost_usd,
            "error": result.error,
            "output": (result.output or "")[:2000],
            "quality": score.overall,
            "score": score.overall,
            "overall": score.overall,
            "correctness": score.correctness,
            "completeness": score.completeness,
            "honesty": score.honesty,
            "conciseness": score.conciseness,
            "kind": "evaluation",
            "metadata": dict(result.metadata),
        }
        try:
            _, shape = _book_effect(payload)
            result.metadata["ledger"] = f"booked:{shape}"
        except Exception as exc:  # scoring must survive a broken ledger
            result.metadata["ledger"] = f"error:{type(exc).__name__}:{_short(exc)}"
