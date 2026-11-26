"""Guard model client (prompt injection / safety classification).

The guard model (Granite Guardian, NeMo Guardrails, Llama Guard, or the client's own)
runs behind a small HTTP wrapper with this contract:

    POST /v1/classify   {"text": "..."}  ->  {"injection": 0.0-1.0, "unsafe": 0.0-1.0,
                                              "model": "granite-guardian-x.y"}

CTO F5: at least two replicas plus an approved fallback endpoint. The client tries
replicas in order, then the fallback; if all fail it raises GuardUnavailableError and
the engine applies the tier fail mode.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

import httpx


class GuardUnavailableError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class GuardScore:
    injection: float
    unsafe: float
    model: str


class GuardModel(Protocol):
    def classify(self, text: str) -> GuardScore: ...


class HttpGuardClient:
    def __init__(
        self,
        endpoints: list[str],
        fallback: str | None = None,
        timeout_s: float = 0.3,
        cache_size: int = 2048,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if len(endpoints) < 1:
            raise ValueError("at least one guard endpoint is required")
        self._urls = [*endpoints, *([fallback] if fallback else [])]
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        self._cache: OrderedDict[str, GuardScore] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def classify(self, text: str) -> GuardScore:
        # Cache by content hash: repeated system prompts / retrieved docs are scored once.
        h = hashlib.sha256(text.encode()).hexdigest()
        with self._lock:
            if h in self._cache:
                self._cache.move_to_end(h)
                return self._cache[h]
        errors: list[str] = []
        for url in self._urls:
            try:
                r = self._client.post(f"{url.rstrip('/')}/v1/classify", json={"text": text})
                r.raise_for_status()
                b = r.json()
                score = GuardScore(
                    float(b["injection"]), float(b["unsafe"]), str(b.get("model", "?"))
                )
                if not (0 <= score.injection <= 1 and 0 <= score.unsafe <= 1):
                    raise ValueError("score out of range")
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                errors.append(f"{url}: {type(e).__name__}")
                continue
            with self._lock:
                self._cache[h] = score
                if len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
            return score
        raise GuardUnavailableError("all guard endpoints failed: " + "; ".join(errors))
