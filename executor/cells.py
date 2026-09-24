"""Tile↔cell isomorphism — spreadsheet porting for receipts (Rung 5).

A ledger row is a cell record: {coord, opcode, payload_hash, value_view}.
A chain segment is a column range. Porting = copying hashes between workbooks.

to_sheet(ledger)  -> {"A1": cell, ...} grid dict (column A = chain order)
from_sheet(grid)  -> replay rows in coord order; verify() must pass.
"""
from __future__ import annotations

import json
from typing import Any

COLUMNS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _coord(index: int) -> str:
    """0 -> A1, 1 -> A2 ... 25 -> A26, 26 -> B1 (column-major: one column = one chain)."""
    return f"A{index + 1}"  # single column for the spine; payload views spill to B.. via views


def to_sheet(ledger) -> dict[str, dict[str, Any]]:
    """Ledger -> grid. A = chain spine (row hash), B = op view, C = actor view, D = score.

    Accepts ReceiptRow dataclasses (executor.ledger) or plain dicts (embedded port).
    """
    grid: dict[str, dict[str, Any]] = {}
    actor = getattr(ledger, "actor", "?")
    for i, row in enumerate(ledger.rows):
        n = i + 1
        if isinstance(row, dict):
            kind = row.get("op", "?")
            body = row.get("payload", {})
            prev = row.get("parent", "")
            rh = row.get("row_hash", "")
        else:
            kind = getattr(row, "kind", "?")
            body = getattr(row, "body", {})
            prev = getattr(row, "prev_hash", "")
            rh = getattr(row, "row_hash", "")
        ph = _hash(body)
        grid[f"A{n}"] = {"opcode": kind, "value": rh or ph,
                         "payload_hash": ph, "parent": prev}
        grid[f"B{n}"] = {"opcode": "VIEW", "value": kind, "view": "op"}
        grid[f"C{n}"] = {"opcode": "VIEW", "value": actor, "view": "actor"}
        if isinstance(body, dict) and "overall" in body:
            grid[f"D{n}"] = {"opcode": "VIEW", "value": body["overall"], "view": "score"}
    return grid


def from_sheet(grid: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Grid -> row payloads in chain order (column A coords sorted numerically)."""
    spine = []
    for coord, cell in grid.items():
        if coord.startswith("A") and cell.get("opcode") not in (None, "VIEW"):
            try:
                n = int(coord[1:])
            except ValueError:
                continue
            spine.append((n, cell))
    spine.sort()
    return [{"op": c["opcode"], "payload_hash": c.get("payload_hash", c.get("value")),
             "parent": c.get("parent"), "view": c.get("value")} for _, c in spine]


def _hash(payload) -> str:
    if isinstance(payload, str):
        data = payload.encode("utf-8")  # bytes-law: strings hash raw, pins match
    else:
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode()
    h = 0xCBF29CE484222325
    for b in data:
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"0x{h:016x}"


def port_segment(ledger, start: int, end: int) -> dict[str, dict[str, Any]]:
    """The copy-paste primitive: a chain slice as a mini-grid, portable as a cell range."""
    grid = to_sheet(ledger)
    return {f"A{i - start}": grid[f"A{i}"]
            for i in range(start + 1, min(end, len(ledger.rows)) + 1) if f"A{i}" in grid}
