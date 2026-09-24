"""The Stranger's Sea-Trial — the ocean is real only if a foreign vessel can sail it.

A ForeignVessel imports NOTHING from executor. It holds only:
  1. ports/embedded/manifest.json  (the chart)
  2. a chain export (plain JSON rows)

It must reproduce the hash recipe from the chart's machine-readable fields,
verify our chain end-to-end, refuse tampering loudly, and extend the chain
with a row OUR substrate re-derives and accepts. If the chart hides knowledge,
the trial fails and we fix the chart — never the stranger.
"""
from __future__ import annotations

import json
import sys
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, "/tmp/quilt-executor")

REPO = Path("/tmp/quilt-executor")
MANIFEST = json.loads((REPO / "ports/embedded/manifest.json").read_text())


class ForeignVessel:
    """Chart-only sailing. Every constant comes from MANIFEST['chain'] /
    MANIFEST['substrates']; no executor import, no fleet knowledge."""

    REQUIRED_KEYS = {
        ("chain", "offset_basis"), ("chain", "prime"), ("chain", "mask"),
        ("chain", "canon"), ("chain", "hash_input_rule"),
        ("chain", "genesis_parent"),
        ("substrates", "executor", "seal_body"),
        ("substrates", "executor", "hash_encoding"),
        ("conformance_vectors",),
    }

    def __init__(self, manifest: dict):
        missing = [k for k in self.REQUIRED_KEYS
                   if self._dig(manifest, k) is None]
        if missing:
            raise ValueError(f"chart insufficient, missing: {missing}")
        self.m = manifest
        c = manifest["chain"]
        self.offset = int(c["offset_basis"], 16)
        self.prime = int(c["prime"], 16)
        self.mask = int(c["mask"], 16)
        self.canon = c["canon"]

    @staticmethod
    def _dig(d, path):
        cur = d
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return None
            cur = cur[p]
        return cur

    def _canon(self, obj) -> bytes:
        spec = self.canon
        return json.dumps(obj, sort_keys=spec["sort_keys"],
                          separators=tuple(spec["separators"]),
                          ensure_ascii=spec["ensure_ascii"]
                          ).encode(spec["encoding"])

    def h_int(self, data: bytes) -> int:
        h = self.offset
        for b in data:
            h = ((h ^ b) * self.prime) & self.mask
        return h

    def hash(self, obj) -> str:
        """hash_input_rule: strings are raw utf-8; everything else canon first.
        Executor substrate encodes hex16 lowercase with NO 0x prefix."""
        data = obj.encode(self.canon["encoding"]) if isinstance(obj, str) else self._canon(obj)
        return f"{self.h_int(data):016x}"

    def verify_vector(self) -> bool:
        for text, expect in self.m["conformance_vectors"].items():
            got = "0x" + self.hash(text)
            if got != expect:
                return False
        return True

    def verify_chain(self, rows: list[dict]):
        """Executor-schema verification from the chart alone. Loud, indexed refusal."""
        enc = self.m["substrates"]["executor"]["hash_encoding"]
        genesis = "0" * 16
        prev = genesis
        for i, r in enumerate(rows):
            if r["prev_hash"] != prev:
                return False, f"row {i}: parent linkage broken"
            seal = {"seq": r["seq"], "prev": r["prev_hash"],
                    "kind": r["kind"], "body": r["body"]}
            expect = self.hash(seal) if "no 0x" in enc.lower() else "0x" + self.hash(seal)
            if r["row_hash"] != expect:
                return False, f"row {i}: row_hash mismatch (tamper or foreign recipe)"
            prev = r["row_hash"]
        return True, f"{len(rows)} rows verified"

    def compose_row(self, seq: int, prev_hash: str, kind: str, body: dict) -> dict:
        """A foreign-built row, sealed by chart recipe, ready for native acceptance."""
        seal = {"seq": seq, "prev": prev_hash, "kind": kind, "body": body}
        return {"seq": seq, "prev_hash": prev_hash, "kind": kind, "body": body,
                "row_hash": self.hash(seal)}

    def build_chain(self, entries: list) -> list:
        """Full foreign genesis chain from (kind, body) entries — chart recipe only."""
        rows = []
        prev = "0" * 16
        for i, (kind, body) in enumerate(entries, start=1):
            row = self.compose_row(i, prev, kind, body)
            rows.append(row)
            prev = row["row_hash"]
        return rows

    def self_verify(self, rows: list):
        return self.verify_chain(rows)


def _export_native(rows) -> list[dict]:
    return [asdict(r) for r in rows]


class TestForeignVessel(unittest.TestCase):
    def test_chart_reproduces_conformance_vector(self):
        v = ForeignVessel(MANIFEST)
        self.assertTrue(v.verify_vector())

    def test_stranger_verifies_native_chain(self):
        from executor.ledger import Ledger
        led = Ledger(name="harbor")
        led.book_tick({"n": 1})
        led.book_tick({"n": 2})
        led.book_refused(__import__("executor.ledger", fromlist=["TaskRequest"])
                         .TaskRequest(task_id="x", prompt=""), "p", "why")
        v = ForeignVessel(MANIFEST)
        ok, msg = v.verify_chain(_export_native(led.rows))
        self.assertTrue(ok, msg)

    def test_tamper_is_loud_refusal_with_index(self):
        from executor.ledger import Ledger
        led = Ledger(name="harbor")
        led.book_tick({"n": 1})
        led.book_tick({"n": 2})
        rows = _export_native(led.rows)
        rows[1]["body"]["summary"]["n"] = 999  # tamper
        v = ForeignVessel(MANIFEST)
        ok, msg = v.verify_chain(rows)
        self.assertFalse(ok)
        self.assertIn("row 1", msg)

    def test_foreign_row_accepted_by_native_chain(self):
        """The ocean moment: stranger extends, native substrate re-derives + accepts."""
        from executor.ledger import Ledger, ReceiptRow, canonical_hash
        led = Ledger(name="harbor")
        led.book_tick({"n": 1})
        v = ForeignVessel(MANIFEST)
        foreign = v.compose_row(seq=len(led.rows) + 1,
                                prev_hash=led.rows[-1].row_hash,
                                kind="EFFECT",
                                body={"vessel": "foreign", "catch": "one current"})
        # native re-derivation: same recipe, same answer
        self.assertEqual(foreign["row_hash"],
                         canonical_hash({"seq": foreign["seq"], "prev": foreign["prev_hash"],
                                         "kind": foreign["kind"], "body": foreign["body"]}))
        # accept into the native chain as a real row
        row = ReceiptRow(seq=foreign["seq"], prev_hash=foreign["prev_hash"],
                         kind=foreign["kind"], body=foreign["body"])
        row.row_hash = foreign["row_hash"]
        led.rows.append(row)
        ok, problems = led.verify()
        self.assertTrue(ok, problems)

    def test_chart_sufficiency_contract(self):
        """The vessel declares exactly what it needs; the chart must contain it."""
        for path in ForeignVessel.REQUIRED_KEYS:
            self.assertIsNotNone(ForeignVessel._dig(MANIFEST, path),
                                 f"chart missing {path}")

    def test_reciprocity_native_verifies_foreign_genesis_chain(self):
        """Whole foreign chain is first-class native water: verify() accepts it,
        and a forged foreign chain fails native verification."""
        from executor.ledger import Ledger, ReceiptRow
        v = ForeignVessel(MANIFEST)
        rows = v.build_chain([("BIND", {"vessel": "foreign"}),
                              ("EFFECT", {"catch": 1}),
                              ("REFUSED", {"reason": "no wind"})])
        ok, msg = v.self_verify(rows)
        self.assertTrue(ok, msg)
        led = Ledger(name="foreign-harbor")
        led.rows = [ReceiptRow(seq=r["seq"], prev_hash=r["prev_hash"],
                               kind=r["kind"], body=r["body"],
                               row_hash=r["row_hash"]) for r in rows]
        ok, problems = led.verify()
        self.assertTrue(ok, problems)
        # forged foreign chain must NOT pass native verification
        rows[2]["body"] = {"reason": "forged manifest"}
        led.rows = [ReceiptRow(seq=r["seq"], prev_hash=r["prev_hash"],
                               kind=r["kind"], body=r["body"],
                               row_hash=r["row_hash"]) for r in rows]
        ok, _ = led.verify()
        self.assertFalse(ok)

    def test_harbor_mouth_no_hand_delivery(self):
        """Fetch /chart + /ledger/export live; stranger verifies with ONLY the
        fetched chart. No file paths, no executor imports in the sailing half."""
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from executor.web import Handler, LEDGER
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            chart = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/chart", timeout=5).read())
            export = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/ledger/export", timeout=5).read())
        finally:
            srv.shutdown()
            t.join(timeout=5)
        self.assertEqual(export["genesis"], "0" * 16)
        stranger = ForeignVessel(chart)  # chart came over the wire, nothing local
        self.assertTrue(stranger.verify_vector())
        ok, msg = stranger.verify_chain(export["rows"])
        self.assertTrue(ok, msg)

    def test_peer_seas_two_foreign_vessels(self):
        """Two strangers, zero native code: they cross-verify each other's
        independently-built chains using only the shared chart."""
        a = ForeignVessel(MANIFEST)
        b = ForeignVessel(MANIFEST)
        chain_a = a.build_chain([("EFFECT", {"vessel": "A", "catch": 7})])
        chain_b = b.build_chain([("EFFECT", {"vessel": "B", "catch": 12}),
                                 ("REFUSED", {"reason": "reef"})])
        self.assertNotEqual(chain_a[0]["row_hash"], chain_b[0]["row_hash"])
        self.assertTrue(a.verify_chain(chain_b)[0])
        self.assertTrue(b.verify_chain(chain_a)[0])
        chain_b[1]["body"] = {"reason": "rewritten"}
        self.assertFalse(a.verify_chain(chain_b)[0])


if __name__ == "__main__":
    unittest.main()
