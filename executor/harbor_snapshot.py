"""Harbor snapshot: bake the quilt-executor surface into a portable artifact.

Adopts the quilt-fleet-snapshot convention (MANIFEST.json with sha256_16
hashes, categories, verify) for the executor's own harbor: the chart,
the hull, and the receipts canon. A fresh sandbox restores the harbor
without re-deriving it.
"""
import hashlib
import io
import json
import os
import tarfile
from datetime import datetime, timezone

MANIFEST_NAME = "MANIFEST.json"

# (repo-relative path, category, description) — the harbor surface.
HARBOR_FILES = [
    ("ports/embedded/manifest.json", "chart", "RECEIPTS_V2 canon + discovery"),
    ("ports/embedded/ledger_micro.py", "hull", "embedded ledger (foreign hull)"),
    ("executor/__init__.py", "hull", "executor entry"),
    ("executor/ledger.py", "hull", "custody ledger (native hull)"),
    ("executor/witness.py", "hull", "witness bridge (registry envelopes)"),
    ("executor/gravity.py", "hull", "gravity field (moth/jepa/jev cells)"),
    ("executor/qcell.py", "hull", "quantum cell (qrng/kernel)"),
    ("executor/web.py", "hull", "harbor mouth (GET /chart, /ledger/export)"),
    ("executor/providers.py", "hull", "provider roster + refusals"),
    ("README.md", "doctrine", "stranger-first quickstart"),
]

IGNORE_MISSING = True  # a chart entry for a file not yet built is drift, not error


def _sha256_16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _read(repo_root: str, rel: str) -> bytes:
    with open(f"{repo_root}/{rel}", "rb") as f:
        return f.read()


def build(repo_root: str, out_path: str, extra_files=None) -> dict:
    """Bake the harbor surface into a tarball; return the manifest."""
    entries = list(HARBOR_FILES) + list(extra_files or [])
    files = []
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest_stub = {
            "name": "quilt-executor-harbor",
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "files": files,
        }
        for rel, category, description in entries:
            try:
                data = _read(repo_root, rel)
            except FileNotFoundError:
                if not IGNORE_MISSING:
                    raise
                files.append({"path": rel, "category": category,
                              "description": description, "missing": True})
                continue
            info = tarfile.TarInfo(name=f"harbor/{rel}")
            info.size = len(data)
            info.mtime = 0  # reproducible
            tar.addfile(info, io.BytesIO(data))
            files.append({
                "path": rel, "category": category, "description": description,
                "sha256_16": _sha256_16(data), "size_bytes": len(data),
            })
        payload = json.dumps(manifest_stub, sort_keys=True, indent=2).encode()
        info = tarfile.TarInfo(name=MANIFEST_NAME)
        info.size = len(payload)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(payload))
    blob = buf.getvalue()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(blob)
    return {"files": len(files), "bytes": len(blob),
            "sha256_16": _sha256_16(blob)}


def verify(snapshot_path: str) -> dict:
    """Re-hash every member against the baked manifest. Tamper → loud."""
    with tarfile.open(snapshot_path, "r:gz") as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar.getmembers()
                   if m.isfile()}
    raw = members.pop(f"harbor/{MANIFEST_NAME}", members.pop(MANIFEST_NAME, None))
    if raw is None:
        return {"ok": False, "error": "no manifest in snapshot"}
    manifest = json.loads(raw)
    verified, failures = 0, []
    for entry in manifest["files"]:
        if entry.get("missing"):
            continue
        data = members.get(f"harbor/{entry['path']}")
        if data is None:
            failures.append(f"{entry['path']}: absent from snapshot")
        elif _sha256_16(data) != entry["sha256_16"]:
            failures.append(
                f"{entry['path']}: hash mismatch "
                f"({_sha256_16(data)} != {entry['sha256_16']})")
        else:
            verified += 1
    return {"ok": not failures, "verified_files": verified,
            "failures": failures, "file_count": len(manifest["files"])}
