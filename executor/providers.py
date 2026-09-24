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


class JevProviderBase:
    """Shared shape for the two live arms below (kept tiny on purpose)."""

    def estimate_cost(self, request: TaskRequest) -> float:
        return 0.001

    def estimate_latency_ms(self, request: TaskRequest) -> float:
        return 30_000.0


class TypeSafeProvider(JevProviderBase):
    """typesafe.ai JEV oracle — the canon judge, live.

    Contract (task_type="jev-gate"): request.prompt is the artifact text,
    request.context["question"] is the yes/no claim to score. Returns the
    noul probability as output; usage + model ride metadata.
    Keys accepted in order: TYPESAFE_API_KEY, TYPESAFEAI_KEY (Casey's
    handoff name — both spellings live).
    """

    name = "typesafe"
    API = "https://api.typesafe.ai/v1/systemone"

    def _key(self) -> str:
        return (os.environ.get("TYPESAFE_API_KEY")
                or os.environ.get("TYPESAFEAI_KEY") or "")

    def available(self, request: TaskRequest) -> tuple[bool, str]:
        if not self._key():
            return False, "TYPESAFE_API_KEY unset (slot reserved)"
        return True, "ok"

    def execute(self, request: TaskRequest) -> TaskResult:
        ok, why = self.available(request)
        if not ok:
            return TaskResult("", self.name, 0.0, 0.0, error=f"refused:{why}")
        question = request.context.get(
            "question",
            "Does this content describe quilt-native, receipt-chained, "
            "deterministic fleet engineering? Answer yes or no.")
        start = time.monotonic()
        try:
            body = json.dumps({
                "state": request.prompt,
                "model": request.context.get("model", "jev-latest"),
                "questions": {"x": {"type": "noul", "instructions": question}},
            }).encode()
            req = urllib.request.Request(
                self.API, method="POST", data=body,
                headers={"Authorization": f"Bearer {self._key()}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read())
            ans = payload.get("answers", {}).get("x", {})
            noul = ans.get("noul")
            return TaskResult(str(noul), self.name,
                              (time.monotonic() - start) * 1000,
                              self.estimate_cost(request),
                              metadata={"model": payload.get("model"),
                                        "usage": payload.get("usage", {})})
        except Exception as e:
            return TaskResult("", self.name, (time.monotonic() - start) * 1000,
                              0.0, error=str(e)[:200])


class MothQuantumProvider(JevProviderBase):
    """mothquantum.com quantum engines — certified RNG, OTOC chaos probes.

    Contract (task_type="quantum-job"): request.context carries
    {"engine": str, "params": dict, "poll_s": float, "max_polls": int}.
    Returns the result JSON as output; job_id + fingerprints ride metadata.
    The API sits behind a bot filter: browser User-Agent is REQUIRED
    (bare urllib gets Cloudflare 403 / error code 1010 — field-verified).
    Keys accepted in order: MOTHQUANTUM_API_KEY, MOTHQUANTUM_KEY.
    """

    name = "mothquantum"
    API = "https://api.mothquantum.com/api/v1"
    UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

    def _key(self) -> str:
        return (os.environ.get("MOTHQUANTUM_API_KEY")
                or os.environ.get("MOTHQUANTUM_KEY") or "")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key()}",
                "User-Agent": self.UA, "Accept": "application/json"}

    def _get(self, path: str):
        with urllib.request.urlopen(
                urllib.request.Request(self.API + path, headers=self._headers()),
                timeout=30) as resp:
            return json.loads(resp.read())

    def available(self, request: TaskRequest) -> tuple[bool, str]:
        if not self._key():
            return False, "MOTHQUANTUM_API_KEY unset (slot reserved)"
        return True, "ok"

    def execute(self, request: TaskRequest) -> TaskResult:
        ok, why = self.available(request)
        if not ok:
            return TaskResult("", self.name, 0.0, 0.0, error=f"refused:{why}")
        engine = request.context.get("engine", "coin-toss-v1")
        params = request.context.get("params", {})
        poll_s = float(request.context.get("poll_s", 2))
        max_polls = int(request.context.get("max_polls", 90))
        start = time.monotonic()
        try:
            submit = urllib.request.Request(
                f"{self.API}/engines/{engine}/process", method="POST",
                headers={**self._headers(), "Content-Type": "application/json"},
                data=json.dumps({"params": params}).encode())
            with urllib.request.urlopen(submit, timeout=60) as resp:
                job_id = json.loads(resp.read())["job_id"]
            status = "queued"
            for _ in range(max_polls):
                status = self._get(f"/jobs/{job_id}/status")["status"]
                if status in ("completed", "failed", "cancelled"):
                    break
                if (time.monotonic() - start) * 1000 > request.max_latency_ms:
                    return TaskResult("", self.name, (time.monotonic() - start) * 1000,
                                      0.0, error=f"latency budget exhausted polling {job_id}")
                time.sleep(poll_s)
            if status != "completed":
                return TaskResult("", self.name, (time.monotonic() - start) * 1000,
                                  0.0, error=f"job {job_id} terminal status {status}")
            result = self._get(f"/jobs/{job_id}/result")
            out = json.dumps(result, sort_keys=True)
            meta = {"engine": engine, "job_id": job_id}
            bell = (result.get("result", {}).get("output", {}).get("bell_witness")
                    or {})
            if bell.get("S") is not None:
                meta["bell_s"] = bell["S"]
            return TaskResult(out, self.name, (time.monotonic() - start) * 1000,
                              self.estimate_cost(request), metadata=meta)
        except Exception as e:
            return TaskResult("", self.name, (time.monotonic() - start) * 1000,
                              0.0, error=str(e)[:200])


def default_roster() -> list:
    """The full arm set: live + CLI + the two reserved slots, now LIVE.

    EnvSlotProvider stays for future slots; typesafe/mothquantum graduated
    to real executors the day the keys landed (2026-09-25). The refusal
    wording is pin-contractual — test_core asserts it verbatim.
    """
    return [
        KimiProvider(),
        CliProvider("claude", "claude"),
        CliProvider("crush", "crush"),
        TypeSafeProvider(),
        MothQuantumProvider(),
    ]
