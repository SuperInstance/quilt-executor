"""Canonical receipt ledger for quilt-executor — family recipe.

canonical JSON (sorted keys, compact, UTF-8) + FNV-1a-64 chain.
Every decision the executor makes is a row: rivals preserved (MV-honesty),
refusals hash-committed, boot re-verifies. Same recipe as laya4quilt /
tagseq2tagseq4quilt / gpu_bpe4quilt — any family substrate verifies these rows.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
FNV_MASK = 0xFFFFFFFFFFFFFFFF
GENESIS = "0" * 16


def fnv1a64(data: bytes) -> int:
    h = FNV_OFFSET
    for b in data:
        h ^= b
        h = (h * FNV_PRIME) & FNV_MASK
    return h


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(obj: Any) -> str:
    return f"{fnv1a64(canonical(obj).encode('utf-8')):016x}"


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass
class TaskRequest:
    task_id: str
    prompt: str
    task_type: str = "general"
    context: dict = field(default_factory=dict)
    max_cost_usd: float = 0.10
    max_latency_ms: float = 120_000.0
    quality_floor: float = 0.0


@dataclass
class TaskResult:
    output: str
    provider: str
    latency_ms: float
    cost_usd: float
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    quality: Optional[float] = None


@dataclass
class ReceiptRow:
    seq: int
    prev_hash: str
    kind: str  # BIND | EFFECT | REFUSED | TICK
    body: dict
    row_hash: str = ""

    def seal(self) -> None:
        self.row_hash = canonical_hash(
            {"seq": self.seq, "prev": self.prev_hash, "kind": self.kind, "body": self.body}
        )


class Ledger:
    """Append-only hash-chained receipt log. Tamper = loud refusal at verify."""

    def __init__(self, name: str = "executor"):
        self.name = name
        self.rows: list[ReceiptRow] = []

    def _next(self, kind: str, body: dict) -> ReceiptRow:
        prev = self.rows[-1].row_hash if self.rows else GENESIS
        row = ReceiptRow(seq=len(self.rows) + 1, prev_hash=prev, kind=kind, body=body)
        row.seal()
        self.rows.append(row)
        return row

    def book(self, kind: str, body: dict) -> ReceiptRow:
        """Public generic booking — the extension point for new row kinds."""
        return self._next(kind, body)

    def book_bind(self, request: TaskRequest) -> ReceiptRow:
        """Intent filed. The task exists because this row says so."""
        return self._next("BIND", {
            "task_id": request.task_id,
            "task_type": request.task_type,
            "prompt_sha": sha256_hex(request.prompt)[:16],
            "budget": {"cost": request.max_cost_usd, "latency_ms": request.max_latency_ms},
        })

    def book_effect(self, request: TaskRequest, chosen: TaskResult,
                    quality: float, rivals: list[TaskResult]) -> ReceiptRow:
        """Decision receipt. ALL rival utilities preserved — losers are evidence."""
        return self._next("EFFECT", {
            "task_id": request.task_id,
            "chosen": {"provider": chosen.provider, "quality": quality,
                       "latency_ms": chosen.latency_ms, "cost_usd": chosen.cost_usd},
            "rivals": [{"provider": r.provider,
                        "quality": r.quality,
                        "error": r.error} for r in rivals],
        })

    def book_refused(self, request: TaskRequest, provider: str, reason: str) -> ReceiptRow:
        """A gate said no. Visible forever, hash-committed."""
        return self._next("REFUSED", {
            "task_id": request.task_id,
            "provider": provider,
            "reason": reason,
        })

    def book_tick(self, summary: dict) -> ReceiptRow:
        return self._next("TICK", {"summary": summary, "ts": time.time()})

    def verify(self) -> tuple[bool, list[str]]:
        """Full re-derivation. False + reasons, never an exception."""
        problems: list[str] = []
        prev = GENESIS
        for i, row in enumerate(self.rows, start=1):
            if row.seq != i:
                problems.append(f"row {i}: seq mismatch {row.seq}")
            if row.prev_hash != prev:
                problems.append(f"row {i}: chain break (prev {row.prev_hash[:8]} != {prev[:8]})")
            expect = canonical_hash(
                {"seq": row.seq, "prev": row.prev_hash, "kind": row.kind, "body": row.body}
            )
            if row.row_hash != expect:
                problems.append(f"row {i}: row_hash recomputes to {expect[:8]}, stored {row.row_hash[:8]}")
            prev = row.row_hash
        return (not problems), problems

    def dump(self) -> str:
        return "\n".join(canonical(asdict(r)) for r in self.rows)

    def refusals(self) -> list[ReceiptRow]:
        """The refusal-shadow index — first-class query surface."""
        return [r for r in self.rows if r.kind == "REFUSED"]
