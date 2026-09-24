"""Execution memory for the router.

A FIFO-bounded cache of scored results, plus the queryable aggregates built
from every :meth:`ExecutionMemory.put` -- ``quality_ledger`` (mean quality per
provider) and ``reflex_table`` (per task-type/provider cell stats), which is
the face the bandit reads.
"""

import hashlib
import statistics
from dataclasses import dataclass, replace
from typing import Optional

try:  # in-package import, with a flat fallback for script-style harnesses
    from .ledger import TaskRequest, TaskResult
except ImportError:  # pragma: no cover
    from ledger import TaskRequest, TaskResult  # type: ignore

#: Suffix appended to ``provider`` on cache hits. Aggregations strip it, so a
#: provider's cached and live executions roll up under one name.
CACHED_SUFFIX = ":cached"

#: Minimum quality for a stored result to serve as a few-shot example.
FEW_SHOT_MIN_QUALITY = 0.7

#: ``request.context`` is unused for keys: identity is prompt + task type.
KEY_SEPARATOR = "|"


def cache_key(prompt: str, task_type: str) -> str:
    """Stable 16-hex key: ``sha256("<prompt>|<task_type>")[:16]``."""
    return hashlib.sha256(f"{prompt}{KEY_SEPARATOR}{task_type}".encode("utf-8")).hexdigest()[:16]


def _strip_cached(provider: str) -> str:
    """Roll a cached provider name back onto its live provider."""
    return provider.removesuffix(CACHED_SUFFIX)


def _tag_cached(provider: str) -> str:
    """Mark a provider name as cached, idempotently (never ``x:cached:cached``)."""
    return provider if provider.endswith(CACHED_SUFFIX) else provider + CACHED_SUFFIX


def _unit(value: float) -> float:
    """Qualities live in [0, 1]; out-of-range input is clipped, not trusted."""
    return max(0.0, min(1.0, float(value)))


@dataclass
class _Entry:
    """One cached result plus the quality it was scored at."""

    key: str
    request: TaskRequest
    result: TaskResult
    quality: float


@dataclass
class _Record:
    """One observed execution, feeding ``quality_ledger`` / ``reflex_table``."""

    task_type: str
    provider: str
    quality: float
    latency_ms: float
    cost_usd: float


class ExecutionMemory:
    """FIFO-bounded result cache plus the aggregates the bandit queries.

    The cache holds at most ``max_size`` results keyed by :func:`cache_key`
    and evicts oldest-inserted first. The aggregate records are deliberately
    *not* evicted with the cache: they are the bandit's face, and shrinking
    them on cache churn would rewrite routing history every time the cache
    turned over.
    """

    def __init__(self, max_size: int = 128) -> None:
        """Create memory holding at most *max_size* cached results."""
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._max_size = max_size
        self._cache: dict[str, _Entry] = {}  # insertion-ordered => FIFO
        self._records: list[_Record] = []
        self.evictions = 0

    def __len__(self) -> int:
        """Number of results currently cached."""
        return len(self._cache)

    def get(self, request: TaskRequest) -> Optional[TaskResult]:
        """The cached result for *request*, or ``None`` on a miss.

        The hit is a fresh copy reporting ``latency_ms=0`` and
        ``provider="<orig>:cached"``, with the stored quality in
        ``metadata["quality"]``. The stored original is never handed out, so
        callers cannot mutate the cache through a returned result.
        """
        key = cache_key(request.prompt, request.task_type)
        entry = self._cache.get(key)
        if entry is None:
            return None
        hit = replace(
            entry.result,
            latency_ms=0.0,
            provider=_tag_cached(entry.result.provider),
        )
        hit.metadata = {
            **entry.result.metadata,
            "cache": "hit",
            "cache_key": key,
            "original_provider": entry.result.provider,
            "quality": entry.quality,
        }
        return hit

    def put(self, request: TaskRequest, result: TaskResult, quality: float) -> str:
        """Store *result* under *request*'s key, evicting FIFO when full.

        Every put also appends an aggregate record, so the reflex table sees
        cached and live executions alike. Quality is clipped into [0, 1].
        Returns the cache key.
        """
        key = cache_key(request.prompt, request.task_type)
        if key not in self._cache and len(self._cache) >= self._max_size:
            del self._cache[next(iter(self._cache))]  # oldest insert leaves first
            self.evictions += 1
        self._cache[key] = _Entry(key, request, result, _unit(quality))
        self._records.append(
            _Record(
                task_type=request.task_type,
                provider=result.provider,
                quality=_unit(quality),
                latency_ms=float(result.latency_ms),
                cost_usd=float(result.cost_usd),
            )
        )
        return key

    def few_shot_examples(self, request: TaskRequest, n: int = 3) -> list[TaskResult]:
        """Up to *n* best cached examples for *request*'s task type.

        Same ``task_type`` with quality strictly above
        :data:`FEW_SHOT_MIN_QUALITY`, highest quality first; ties keep
        insertion order. Like :meth:`get`, the results are copies, so callers
        can prompt-build with ``.output`` without mutating the cache.
        """
        matches = [
            entry
            for entry in self._cache.values()
            if entry.request.task_type == request.task_type
            and entry.quality > FEW_SHOT_MIN_QUALITY
        ]
        matches.sort(key=lambda entry: entry.quality, reverse=True)
        return [replace(entry.result) for entry in matches[: max(0, n)]]

    def quality_ledger(self) -> dict[str, float]:
        """Mean quality per provider, cached hits rolled into the live provider."""
        buckets: dict[str, list[float]] = {}
        for record in self._records:
            buckets.setdefault(_strip_cached(record.provider), []).append(record.quality)
        return {provider: statistics.fmean(qualities) for provider, qualities in buckets.items()}

    def reflex_table(self) -> dict[tuple[str, str], dict[str, float]]:
        """Per ``(task_type, provider)`` means -- the bandit's face.

        Each cell carries ``n``, ``mean_quality``, ``mean_latency`` and
        ``mean_cost`` over every execution recorded for it, cached hits
        included. An empty memory yields an empty table, never fabricated rows.
        """
        cells: dict[tuple[str, str], list[_Record]] = {}
        for record in self._records:
            cells.setdefault((record.task_type, _strip_cached(record.provider)), []).append(record)
        return {
            cell: {
                "n": float(len(rows)),
                "mean_quality": statistics.fmean(row.quality for row in rows),
                "mean_latency": statistics.fmean(row.latency_ms for row in rows),
                "mean_cost": statistics.fmean(row.cost_usd for row in rows),
            }
            for cell, rows in cells.items()
        }
