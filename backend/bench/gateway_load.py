"""Load test for a RUNNING Captain gateway (the real stack: PostgreSQL, OPA, ledger worker).

    .venv\\Scripts\\python -m bench.gateway_load --requests 300 --concurrency 10
    .venv\\Scripts\\python -m bench.gateway_load --stream --requests 100 --concurrency 5

Start the services from the README first (OPA, worker, mock provider, gateway). Use the
MOCK provider (alias claude-sonnet): the model answers instantly, so what you measure is
Captain's own overhead: authentication, policy, guardrails, budget reservation, evidence.
Numbers against a real provider mostly measure that provider.

Reports per-request latency (p50 / p95 / p99), time to first token for streams, throughput,
and outcomes by HTTP status. Refusals (for example a budget running out during the test)
are counted, not hidden. Exit code 1 if any request failed with a server error.

The demo budget is $50: the mock costs a few thousandths of a cent a call, so a few
hundred requests are fine. Prompts are tiny and contain no personal data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

BACKEND = Path(__file__).resolve().parents[1]


@dataclass
class Sample:
    status: int
    seconds: float
    ttft: float | None = None


@dataclass
class Report:
    samples: list[Sample] = field(default_factory=list)
    wall_s: float = 0.0

    def pct(self, p: float, values: list[float]) -> float | None:
        if not values:
            return None
        v = sorted(values)
        return v[min(len(v) - 1, int(p / 100 * (len(v) - 1) + 0.5))]

    def summary(self) -> dict[str, Any]:
        ok = [s for s in self.samples if s.status == 200]
        lat = [s.seconds * 1000 for s in ok]
        ttft = [s.ttft * 1000 for s in ok if s.ttft is not None]
        return {
            "requests": len(self.samples),
            "by_status": dict(sorted(Counter(s.status for s in self.samples).items())),
            "throughput_rps": round(len(self.samples) / self.wall_s, 1) if self.wall_s else None,
            "latency_ms": {
                "p50": self._r(self.pct(50, lat)),
                "p95": self._r(self.pct(95, lat)),
                "p99": self._r(self.pct(99, lat)),
                "max": self._r(max(lat) if lat else None),
                "mean": self._r(statistics.fmean(lat) if lat else None),
            },
            "ttft_ms": {"p50": self._r(self.pct(50, ttft)), "p95": self._r(self.pct(95, ttft))}
            if ttft
            else None,
        }

    @staticmethod
    def _r(v: float | None) -> float | None:
        return None if v is None else round(v, 1)


async def one(client: httpx.AsyncClient, url: str, key: str, model: str, stream: bool) -> Sample:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "load test: say hello"}],
        "max_tokens": 20,
        "stream": stream,
    }
    headers = {"Authorization": f"Bearer {key}"}
    t0 = time.perf_counter()
    try:
        if not stream:
            r = await client.post(f"{url}/v1/chat/completions", json=body, headers=headers)
            return Sample(r.status_code, time.perf_counter() - t0)
        async with client.stream(
            "POST", f"{url}/v1/chat/completions", json=body, headers=headers
        ) as r:
            first: float | None = None
            async for line in r.aiter_lines():
                if first is None and line.startswith("data:") and "[DONE]" not in line:
                    first = time.perf_counter() - t0
            return Sample(r.status_code, time.perf_counter() - t0, first)
    except httpx.HTTPError:
        return Sample(0, time.perf_counter() - t0)  # 0 = no response at all


async def run_load(
    client: httpx.AsyncClient,
    url: str,
    key: str,
    requests: int,
    concurrency: int,
    model: str = "claude-sonnet",
    stream: bool = False,
    warmup: int = 5,
) -> Report:
    for _ in range(warmup):  # connection set-up and first-use costs are not what we measure
        await one(client, url, key, model, stream)
    sem = asyncio.Semaphore(concurrency)
    report = Report()

    async def task() -> None:
        async with sem:
            report.samples.append(await one(client, url, key, model, stream))

    t0 = time.perf_counter()
    await asyncio.gather(*(task() for _ in range(requests)))
    report.wall_s = time.perf_counter() - t0
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--key-file", type=Path, default=BACKEND / ".dev" / "dev-app.key")
    ap.add_argument("--model", default="claude-sonnet")
    ap.add_argument("--requests", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--stream", action="store_true")
    a = ap.parse_args(argv)
    key = a.key_file.read_text(encoding="utf-8").strip()

    async def go() -> Report:
        limits = httpx.Limits(max_connections=a.concurrency + 5)
        async with httpx.AsyncClient(timeout=60.0, limits=limits) as c:
            return await run_load(
                c, a.url.rstrip("/"), key, a.requests, a.concurrency, a.model, a.stream
            )

    report = asyncio.run(go())
    out = report.summary()
    print(json.dumps(out, indent=2))
    failed = sum(1 for s in report.samples if s.status == 0 or s.status >= 500)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
