"""The gateway load tool, run in process against the real gateway app (no network)."""

from __future__ import annotations

import asyncio

import httpx

from bench.gateway_load import Report, Sample, run_load
from tests.unit.test_gateway import gateway


def test_load_run_counts_every_request_and_reports_latency() -> None:
    h, tc, hdr = gateway(budget=100_000_000)
    key = hdr["Authorization"][7:]

    async def go() -> Report:
        transport = httpx.ASGITransport(app=tc.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
            return await run_load(c, "http://gw", key, requests=24, concurrency=6, warmup=2)

    rep = asyncio.run(go())
    s = rep.summary()
    assert s["requests"] == 24 and s["by_status"] == {200: 24}
    assert s["latency_ms"]["p50"] is not None and s["latency_ms"]["p95"] >= s["latency_ms"]["p50"]
    assert s["throughput_rps"] and s["ttft_ms"] is None
    # every call was really governed and charged, including the warm-up
    assert h.budget("org:org-1")["spent"] > 0 and h.budget("org:org-1")["reserved"] == 0


def test_streaming_load_measures_time_to_first_token() -> None:
    _, tc, hdr = gateway(budget=100_000_000)
    key = hdr["Authorization"][7:]

    async def go() -> Report:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=tc.app), base_url="http://gw"
        ) as c:
            return await run_load(c, "http://gw", key, 10, 4, stream=True, warmup=1)

    s = asyncio.run(go()).summary()
    assert s["by_status"] == {200: 10} and s["ttft_ms"] and s["ttft_ms"]["p50"] is not None


def test_refusals_and_failures_are_counted_not_hidden() -> None:
    _, tc, _ = gateway()

    async def go() -> Report:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=tc.app), base_url="http://gw"
        ) as c:
            return await run_load(c, "http://gw", "cpt_live_wrong", 5, 2, warmup=0)

    s = asyncio.run(go()).summary()
    assert s["by_status"] == {401: 5} and s["latency_ms"]["p50"] is None  # only successes are timed


def test_percentiles_are_exact_on_a_known_set() -> None:
    r = Report([Sample(200, x / 1000) for x in range(1, 101)], wall_s=2.0)  # 1..100 ms
    s = r.summary()
    assert s["latency_ms"]["p50"] in (50.0, 51.0) and s["latency_ms"]["p99"] in (99.0, 100.0)
    assert s["latency_ms"]["max"] == 100.0 and s["throughput_rps"] == 50.0
    assert Report().summary()["latency_ms"]["p50"] is None
