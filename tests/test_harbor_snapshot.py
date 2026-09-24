"""Pins for the harbor snapshot (fleet-snapshot convention adoption)."""
import json
import os
import tarfile

from executor import harbor_snapshot as hs


def _repo() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build(tmp_path):
    out = str(tmp_path / "harbor.tar.gz")
    result = hs.build(_repo(), out)
    return out, result


def test_build_bakes_manifest_and_harbor_files(tmp_path):
    out, result = _build(tmp_path)
    assert os.path.getsize(out) == result["bytes"]
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert hs.MANIFEST_NAME in names
    assert "harbor/ports/embedded/manifest.json" in names
    assert "harbor/executor/ledger.py" in names


def test_manifest_records_chart_hull_doctrine_categories(tmp_path):
    out, _ = _build(tmp_path)
    with tarfile.open(out, "r:gz") as tar:
        manifest = json.loads(tar.extractfile(hs.MANIFEST_NAME).read())
    cats = {f["category"] for f in manifest["files"] if not f.get("missing")}
    assert {"chart", "hull", "doctrine"} <= cats
    # every hashed file carries the fleet-snapshot field names
    for f in manifest["files"]:
        if not f.get("missing"):
            assert len(f["sha256_16"]) == 16 and f["size_bytes"] > 0


def test_verify_roundtrip_ok(tmp_path):
    out, _ = _build(tmp_path)
    v = hs.verify(out)
    assert v["ok"] and v["failures"] == []
    assert v["verified_files"] == v["file_count"]


def test_verify_detects_tamper_loudly(tmp_path):
    out, _ = _build(tmp_path)
    # flip one byte inside the ledger member
    with tarfile.open(out, "r:gz") as tin:
        members = [(m, tin.extractfile(m).read() if m.isfile() else None)
                   for m in tin.getmembers()]
    buf_path = str(tmp_path / "tampered.tar.gz")
    with tarfile.open(buf_path, "w:gz") as tout:
        for m, data in members:
            if m.name == "harbor/executor/ledger.py":
                data = b"# tampered\n" + data
            if m.isfile():
                tout.addfile(m, __import__("io").BytesIO(data))
            else:
                tout.addfile(m)
    v = hs.verify(buf_path)
    assert not v["ok"]
    assert any("ledger.py" in f and "mismatch" in f for f in v["failures"])


def test_build_reproducible_same_tree(tmp_path):
    a, _ = _build(tmp_path / "a")
    b, _ = _build(tmp_path / "a")
    # two builds of the same tree → identical member hashes in manifests
    def manifest_hashes(path):
        with tarfile.open(path, "r:gz") as tar:
            m = json.loads(tar.extractfile(hs.MANIFEST_NAME).read())
        return [(f["path"], f["sha256_16"]) for f in m["files"]]
    assert manifest_hashes(a) == manifest_hashes(b)
