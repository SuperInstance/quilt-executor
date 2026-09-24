"""Web surface for quilt-executor — stdlib-only (Rung 3 of the portability ladder).

Receipts over self-report applies to ourselves: every endpoint books an EFFECT row
for its own operation. GET /stream emits newline-delimited receipts (SSE idiom,
zero deps). Fallbacks are visible: any failure books REFUSED with the reason.

Run:  python -m executor.web [port]   (default 8402)
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from executor.ledger import Ledger, TaskRequest, TaskResult
from executor import evaluator as evaluator_mod
from executor.evaluator import Evaluator

PORT = 8402
_CHART_PATH = Path(__file__).resolve().parent.parent / "ports" / "embedded" / "manifest.json"
MANIFEST = json.loads(_CHART_PATH.read_text())  # the harbor chart, served at sea


def _fresh_stack() -> tuple[Ledger, Evaluator]:
    ledger = Ledger(name="quilt-executor-web")
    evaluator_mod.bind_ledger(ledger)
    return ledger, Evaluator()


LEDGER, EVAL = _fresh_stack()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode())

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def log_message(self, *a):  # quiet: the ledger speaks, not the access log
        pass

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/ledger/rows":
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            self._json({"rows": LEDGER.rows[since:], "count": len(LEDGER.rows)})
        elif u.path == "/ledger/verify":
            self._json({"verify": LEDGER.verify()})
        elif u.path == "/chart":
            # the harbor mouth: a stranger needs nothing hand-delivered
            self._json(MANIFEST)
        elif u.path == "/ledger/export":
            # machine-readable chain for foreign vessels (asdict rows, genesis anchor)
            self._json({"rows": [asdict(r) for r in LEDGER.rows],
                        "count": len(LEDGER.rows), "genesis": "0" * 16})
        elif u.path == "/stream":
            self._send(200, self._stream().encode(), "text/plain")
        elif u.path in ("/", "/health"):
            self._json({"ok": True, "rows": len(LEDGER.rows), "name": LEDGER.name})
        else:
            self._json({"error": "unknown route", "path": u.path}, 404)

    def _stream(self) -> str:
        # newline-delimited receipt feed: current chain snapshot then done
        return "".join(json.dumps(r) + "\n" for r in LEDGER.rows)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._read_json()
        t0 = time.monotonic()
        try:
            if u.path == "/tick":
                LEDGER.book_tick({"via": "web", "note": body.get("note", "")})
                self._json({"ticked": True, "rows": len(LEDGER.rows)})
            elif u.path == "/score":
                import asyncio
                req = TaskRequest(task_id=f"web-{len(LEDGER.rows)}",
                                  task_type=body.get("task_type", "score"),
                                  prompt=body.get("prompt", ""))
                res = TaskResult(provider=body.get("provider", "manual"),
                                 output=body.get("output", ""),
                                 latency_ms=0.0, cost_usd=0.0)
                score = asyncio.run(EVAL.evaluate(req, res))
                self._json({"overall": round(score.overall, 4),
                            "axes": {k: getattr(score, k) for k in
                                     ("correctness", "completeness", "honesty", "conciseness")},
                            "rows": len(LEDGER.rows)})
            else:
                self._json({"error": "unknown route", "path": u.path}, 404)
        except Exception as e:  # visible gap, not a silent 500
            LEDGER.book_refused(TaskRequest(task_id=f"web:{u.path}", prompt=""),
                                "web", repr(e))
            self._json({"error": repr(e), "refused": True}, 500)
        finally:
            LEDGER.book_tick({"via": "web", "route": u.path, "self_receipt": True,
                              "ms": round((time.monotonic() - t0) * 1000, 3)})


def main(port: int = PORT) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"quilt-executor web on http://127.0.0.1:{port} — rows={len(LEDGER.rows)}")
    srv.serve_forever()


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else PORT)
