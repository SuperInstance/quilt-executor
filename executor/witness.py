"""Witness bridge — org semantic envelopes booked on the custody chain.

The fleet's quilt-schema-registry standardizes WHAT a receipt means
(polarity ACCEPT/DRIFT/REFUSE, substrate, cell_id, 26 kinds). quilt-executor
standardizes HOW a chain is carried (fnv1a-64, seq/prev seals, chart-sufficient
for strangers). This bridge books registry-style witness envelopes as ledger
rows: one chain, both disciplines, no fork.

If quilt_schema_registry is installed its validator is used; otherwise a
minimal vendored check keeps the bridge dependency-free (visible fallback,
not silent drift — the vendored path books a row noting it).
"""
from __future__ import annotations

import json

from executor.ledger import Ledger, canonical_hash

WITNESS_KIND = "WITNESS"

# Minimal vendored envelope contract (mirror of registry 0.1.0 CANONICAL_FIELDS).
# Used only when quilt_schema_registry is absent; a vendored validation always
# records {"validator": "vendored"} in the row body so the gap is visible.
_ENVELOPE_FIELDS = ("witness_id", "prev_witness_id", "polarity", "substrate",
                    "cell_id", "status", "timestamp", "payload")
_POLARITY = ("ACCEPT", "DRIFT", "REFUSE")


def _validate_envelope(env: dict) -> list[str]:
    try:
        from quilt_schema_registry import validate_receipt  # type: ignore
        ok, errors = validate_receipt(env)
        return [] if ok else errors
    except ImportError:
        errors = []
        for f in _ENVELOPE_FIELDS:
            if f not in env:
                errors.append(f"missing field: {f}")
        if env.get("polarity") not in _POLARITY:
            errors.append(f"polarity must be one of {_POLARITY}")
        if not isinstance(env.get("payload"), dict):
            errors.append("payload must be a dict")
        return errors


def book_witness(ledger: Ledger, envelope: dict) -> "object":
    """Validate a witness envelope and book it on the custody chain.

    Valid -> kind=WITNESS row, body = {"envelope": env, "validator": name}.
    Invalid -> REFUSED row naming every schema error (refuse-don't-book)."""
    from executor.ledger import TaskRequest
    errors = _validate_envelope(envelope)
    validator = "registry" if _registry_available() else "vendored"
    if errors:
        return ledger.book_refused(
            TaskRequest(task_id=f"witness:{envelope.get('witness_id', '?')}",
                        prompt=json.dumps(envelope, sort_keys=True)[:200]),
            "witness_bridge", "; ".join(errors))
    return ledger.book(WITNESS_KIND, {"envelope": envelope, "validator": validator})


def _registry_available() -> bool:
    try:
        import quilt_schema_registry  # noqa: F401
        return True
    except ImportError:
        return False


def witness_export(ledger: Ledger) -> list[dict]:
    """Extract witness envelopes from WITNESS rows for registry-side
    validate_chain() — the semantic view of the custody chain."""
    out = []
    for r in ledger.rows:
        if r.kind == WITNESS_KIND and isinstance(r.body, dict):
            env = r.body.get("envelope")
            if isinstance(env, dict):
                out.append(env)
    return out


def witness_continuity(ledger: Ledger) -> tuple[bool, str]:
    """Cross-discipline continuity: the custody chain's WITNESS rows must be
    ordered so each envelope's prev_witness_id links to the previous witness."""
    envs = witness_export(ledger)
    prev = ""
    for i, e in enumerate(envs):
        if e.get("prev_witness_id", "") != prev:
            return False, f"witness[{i}] continuity broken"
        prev = e.get("witness_id", "")
    return True, f"{len(envs)} witnesses continuous"


def witness_id_recipe() -> dict:
    """The gap this bridge exposes: registry 0.1.1 documents witness_id as
    'sha256-of-canonical' without specifying the canonicalization. Until the
    registry ships a hash recipe, this is the bridge's pinned recipe — the
    same machine-readable canon spec style as RECEIPTS_V2."""
    return {"algorithm": "sha256", "canon": {"format": "json", "sort_keys": True,
            "separators": [",", ":"], "ensure_ascii": False, "encoding": "utf-8"},
            "input": "envelope dict", "output": "hex64 lowercase, no 0x"}


def compute_witness_id(envelope: dict) -> str:
    import hashlib
    spec = witness_id_recipe()["canon"]
    blob = json.dumps(envelope, sort_keys=spec["sort_keys"],
                      separators=tuple(spec["separators"]),
                      ensure_ascii=spec["ensure_ascii"]).encode(spec["encoding"])
    return hashlib.sha256(blob).hexdigest()
