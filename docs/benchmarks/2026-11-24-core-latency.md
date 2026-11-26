# Captain Core latency benchmark — 2026-11-24

- **Script:** `backend/bench/core_latency.py`
- **Run it:** `.venv\Scripts\python -m bench.core_latency --n 500 --threads 8`
- **Environment:**
  - one developer laptop (12 logical CPUs, Windows 11)
  - Captain, PostgreSQL 18 and OPA 1.0.0 all running locally
  - Python 3.12, a single Captain process unless stated otherwise
- **Not a production measurement.** The numbers show where the time goes. The CTO targets must still be confirmed at pilot traffic on pilot infrastructure.

## Per-request added latency (sequential, 500 calls per scenario)

| Scenario | p50 ms | p95 ms | p99 ms | CTO target (p95) | Result |
|---|---|---|---|---|---|
| A. `decide`, no content (kill switch + OPA + evidence write) | 2.7 | 3.5 | 4.1 | ≤ 50 | PASS |
| B. `decide` + regex guardrails, 1 KB prompt | 3.3 | 4.9 | 6.8 | ≤ 50 | PASS |
| C. `decide` + regex + Presidio NER (large model), 1 KB | 28.2 | 30.4 | 33.7 | ≤ 250 | PASS |
| D. full path with Presidio: `decide` + `reserve` + `settle` | 35.0 | 45.7 | 57.5 | ≤ 250 | PASS |
| D′. full path, regex only | 10.6 | 16.2 | 18.0 | — | — |

- **Ledger writer:** 4,798 entries per second sealed (hash-chained), signed and inserted. All 4,710 queued entries verified afterwards.

## What the numbers say
- **Without the guard model, Captain adds about 5 ms at p95** for a decision, and about 16 ms for the full reserve-and-settle path. Both are well inside the 50 ms target.
- **Presidio is the largest single cost: about 25 ms** on a 1 KB prompt, roughly linear in text length. It's inside the 250 ms target. Very long prompts (retrieved documents) should be measured separately.
- **The guard model is NOT included.** It isn't hosted yet; a zero-latency stand-in was used. The CTO table estimates 30–150 ms on GPU. Add that to row C for Tier 1–3. That will still fit the 250 ms target, unless the guard model runs on CPU or on a slow hosted endpoint.

## Throughput and concurrency

| Setup | Governed calls/s (full path, regex) |
|---|---|
| 1 process, 1 thread | 83–94 |
| 1 process, 8 threads | 105 (p95 rises to 113 ms) |
| 4 processes | 238 |
| 8 processes | 292 |

**Threads don't help, processes do.** In one Python process, the global interpreter lock lets only one thread run Python code at a time: `decide` gave 294/s on 1 thread and 298/s on 8. Production must therefore scale with **several worker processes per pod** (for example `uvicorn --workers N`) and more pods, not with more threads.

The flattening from 4 to 8 processes on this laptop has three causes:
1. PostgreSQL and OPA share the same CPUs.
2. Every call locks the same organisation budget row. This is by design: a hard cap needs serialised updates per budget.
3. Every `decide` queries the kill-switch table.

## Follow-ups (not yet done)
1. **Cache kill-switch state in memory** and invalidate it through the PostgreSQL event bus, with a ≤ 5 s TTL as backstop. That removes one database round trip per decision.
2. **Measure at pilot scale:** separate PostgreSQL and OPA hosts, several Captain pods, a real guard model, and streamed responses (time to first token, CTO F3).
3. **If very high per-budget throughput is ever needed,** consider sharded or pre-allocated budget slices for organisation-wide caps. It isn't needed at pilot volumes.
