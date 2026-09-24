"""MicroPython-subset ledger for embedded cells (Rung 4, ports/embedded).

Pure stdlib, no dataclasses, no asyncio, no typing. Same recipe as the full
substrate: 5 opcodes + FNV-1a-64 chain. Conformance vector:

    fnv1a64("café Δ 日本語") == 0x24a555471370b18d

A row booked here verifies on the Python substrate and vice versa — the chain
is the portable format (jeviter lattice doctrine).
"""
import json
import time

FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
MASK64 = 0xFFFFFFFFFFFFFFFF

GENESIS = "0x" + "0" * 16


def fnv1a64(data):
    if isinstance(data, str):
        data = data.encode("utf-8")  # bytes-law: strings hash raw
    elif not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode()
    h = FNV_OFFSET
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & MASK64
    return "0x%016x" % h


class Ledger:
    """Hash-chained receipt ledger. Rows: {op, payload, parent, payload_hash, row_hash, tick}."""

    def __init__(self, actor="embedded-cell"):
        self.actor = actor
        self.rows = []
        self._tick = 0

    def _book(self, op, payload):
        self._tick += 1
        parent = self.rows[-1]["row_hash"] if self.rows else GENESIS
        body = {"op": op, "actor": self.actor, "tick": self._tick,
                "parent": parent, "payload": payload}
        ph = fnv1a64(payload if payload is not None else {})
        body["payload_hash"] = ph
        body["row_hash"] = fnv1a64(body)
        self.rows.append(body)
        return body

    # opcodes: BIND / LINK / EFFECT / VIEW / TICK (+FORGET)
    def bind(self, payload):   return self._book("BIND", payload)
    def link(self, payload):   return self._book("LINK", payload)
    def effect(self, payload): return self._book("EFFECT", payload)
    def view(self, payload):   return self._book("VIEW", payload)
    def tick(self, payload=None):
        p = {"note": "tick"} if payload is None else payload
        return self._book("TICK", p)
    def refused(self, task_id, provider, why):
        return self._book("REFUSED", {"task_id": task_id, "provider": provider, "why": why})

    def verify(self):
        prev = GENESIS
        for i, r in enumerate(self.rows):
            if r["parent"] != prev:
                return False, i
            if r["payload_hash"] != fnv1a64(r["payload"] if r["payload"] is not None else {}):
                return False, i
            body = {k: r[k] for k in ("op", "actor", "tick", "parent", "payload")}
            body["payload_hash"] = r["payload_hash"]
            if r["row_hash"] != fnv1a64(body):
                return False, i
            prev = r["row_hash"]
        return True, len(self.rows)


def demo():
    assert fnv1a64("café Δ 日本語") == "0x24a555471370b18d", "conformance pin"
    led = Ledger(actor="esp32-cell")
    led.bind({"cell": "demo", "lang": "microPython"})
    led.effect({"sensor": "temp", "value": 21.5})
    led.tick()
    ok, n = led.verify()
    print("verify:", ok, "rows:", n)
    return led


if __name__ == "__main__":
    demo()
