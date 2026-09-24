"""Provider adapters. The executor does not know or care what is behind one.

Every gate (missing key, missing GPU, missing CLI) books a REFUSAL row —
capability is asked, not assumed. TYPESAFE/MOTHQUANTUM slots exist per
Casey's pointer; unset = visible refusal, never silent skip.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
from typing import Optional, Protocol

from .ledger import TaskRequest, TaskResult, ReceiptRow


class Provider(Protocol):
    name: str

    def available(self, request: TaskRequest) -> tuple[bool, str]: ...
    def execute(self, request: TaskRequest) -> TaskResult: ...
    def estimate_cost(self, request: TaskRequest) -> float: ...
    def estimate_latency_ms(self, request: TaskRequest) -> float: ...


class KimiProvider:
    """Real KIMI_API_KEY call. The key goes to the turbulence edge on purpose."""

    name = "kimi"
    # Kimi coding gateway (the auth path KIMI_API_KEY is actually scoped for);
    # api.moonshot.ai direct calls 401 with this key — verified by smoke.
    API = "https://agent-gw.kimi.com/coding/v1/chat/completions"

    def __init__(self, model: str = "kimi-k2-0711-preview", max_tokens: int = 256):
        self.model = model
        self.max_tokens = max_tokens

    def available(self, request: TaskRequest) -> tuple[bool, str]:
        if not os.environ.get("KIMI_API_KEY"):
            return False, "KIMI_API_KEY unset"
        return True, "ok"

    def execute(self, request: TaskRequest) -> TaskResult:
        ok, why = self.available(request)
        if not ok:
            return TaskResult("", self.name, 0.0, 0.0, error=f"refused:{why}")
        start = time.monotonic()
        try:
            req = urllib.request.Request(
                self.API,
                data=json.dumps({
                    "model": self.model,
                    "messages": [{"role": "user", "content": request.prompt}],
                    "max_tokens": self.max_tokens,
                    # coding gateway pins temperature=1 for this model (400 otherwise)
                    "temperature": 1,
                }).encode(),
                headers={"Authorization": f"Bearer {os.environ['KIMI_API_KEY']}",
                         "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read())
            ch = payload["choices"][0]
            out = ch["message"].get("content") or ""
            usage = payload.get("usage", {})
            meta = {
                "finish_reason": ch.get("finish_reason"),
                "reasoning_tokens": usage.get("completion_tokens_details", {}).get("reasoning_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
            return TaskResult(out, self.name, (time.monotonic() - start) * 1000,
                              self.estimate_cost(request), metadata=meta)
        except Exception as e:  # network failure is a result, not a crash
            return TaskResult("", self.name, (time.monotonic() - start) * 1000,
                              0.0, error=str(e)[:200])

    def estimate_cost(self, request: TaskRequest) -> float:
        return 0.000_3 + len(request.prompt) * 2.0 / 1_000_000

    def estimate_latency_ms(self, request: TaskRequest) -> float:
        return 15_000.0


class CliProvider:
    """claude / crush as executors. Presence probed on PATH; absence = refusal row."""

    def __init__(self, name: str, cmd: str, arg_style: str = "prompt-file"):
        self.name = name
        self.cmd = cmd
        self.arg_style = arg_style

    def available(self, request: TaskRequest) -> tuple[bool, str]:
        if not shutil.which(self.cmd):
            return False, f"{self.cmd} not on PATH"
        return True, "ok"

    def execute(self, request: TaskRequest) -> TaskResult:
        ok, why = self.available(request)
        if not ok:
            return TaskResult("", self.name, 0.0, 0.0, error=f"refused:{why}")
        start = time.monotonic()
        try:
            if self.name == "claude":
                argv = [self.cmd, "-p", request.prompt, "--permission-mode", "acceptEdits"]
            else:
                argv = [self.cmd, "run", request.prompt]
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
            out = proc.stdout.strip()
            if proc.returncode != 0:
                return TaskResult("", self.name, (time.monotonic() - start) * 1000,
                                  0.0, error=f"rc={proc.returncode}: {proc.stderr[:200]}")
            return TaskResult(out, self.name, (time.monotonic() - start) * 1000,
                              self.estimate_cost(request))
        except Exception as e:
            return TaskResult("", self.name, (time.monotonic() - start) * 1000,
                              0.0, error=str(e)[:200])

    def estimate_cost(self, request: TaskRequest) -> float:
        return 0.005  # CLI session amortized

    def estimate_latency_ms(self, request: TaskRequest) -> float:
        return 240_000.0


class EnvSlotProvider:
    """Named API slot (typesafe.ai / MOTHquantum / any future key).

    Registered so the decider SEES the arm and the ledger records the refusal.
    The day the key lands, the arm is live with history already in the chain.
    """

    def __init__(self, name: str, env_var: str, endpoint: str = ""):
        self.name = name
        self.env_var = env_var
        self.endpoint = endpoint

    def available(self, request: TaskRequest) -> tuple[bool, str]:
        if not os.environ.get(self.env_var):
            return False, f"{self.env_var} unset (slot reserved)"
        if not self.endpoint:
            return False, f"{self.name}: endpoint not configured"
        return True, "ok"

    def execute(self, request: TaskRequest) -> TaskResult:
        ok, why = self.available(request)
        return TaskResult("", self.name, 0.0, 0.0, error=f"refused:{why}")

    def estimate_cost(self, request: TaskRequest) -> float:
        return 0.001

    def estimate_latency_ms(self, request: TaskRequest) -> float:
        return 30_000.0


def default_roster() -> list:
    """The full arm set: live + CLI + reserved slots. Decider sees all."""
    return [
        KimiProvider(),
        CliProvider("claude", "claude"),
        CliProvider("crush", "crush"),
        EnvSlotProvider("typesafe", "TYPESAFE_API_KEY"),
        EnvSlotProvider("mothquantum", "MOTHQUANTUM_API_KEY"),
    ]
