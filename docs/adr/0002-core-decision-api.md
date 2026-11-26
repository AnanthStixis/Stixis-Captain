# ADR-002: Core Decision API contract

- Status: Proposed (to be frozen at the end of Phase 1, week 1)
- Date: 2026-11-24
- Code: `backend/captain/core/decision_api.py`, `backend/captain/core/models.py`

## Context
Captain Core needs a single, stable interface that every integration calls, in-process (API Gateway, MCP Proxy) and over HTTP (extensions, desktop agent, telemetry hooks). The CTO review sets requirements this interface must make explicit: specific reason codes (F14), atomic budget reservation (F6), approval binding and single use (F7), and refusing Tier 1–2 actions when evidence can't be durably recorded (F4).

## Decision
The Core exposes these operations:

| Operation | Core modules | Contract rules |
|---|---|---|
| `resolve(credential, hints)` | Identity, Project Context | Revoked → `KEY_REVOKED`. A conflicting project hint raises `ContextUnresolvedError`: context is **never silently reassigned**. |
| `decide(request)` | Policy, Guardrails, kill switch | Returns `Decision` with one of six types, one specific `ReasonCode`, explanation, policy bundle + version, tier, limits, optional redacted contents / approval ticket, `fail_open_applied`. |
| `reserve(decision, estimate)` | Budget | Only for decisions that permit execution. Reserves the upper-bound estimate atomically on **every** applicable budget level, or none. |
| `settle(reservation, actual)` / `release(reservation)` | Budget, Usage | Charges actual cost and releases the remainder. Each reservation settles exactly once. |
| `request_approval` / `approval_status` / `consume_approval` | Approval Engine | Ticket bound to SHA-256 of canonical arguments. Expiry → `EXPIRED` (refusal). Approval is consumable once (`CONSUMED`). Kill switch → `CANCELLED`. |
| `record(event)` | Usage Collector, Evidence Ledger | Returns a `LedgerReceipt` only after the event is durably queued. Raises `EvidenceUnavailableError` otherwise; integrations must then refuse Tier 1–2 actions. Accepts OBSERVED events with no decision. |

Shared rules:
- Money is integer micro-units.
- Content may be passed inline for inspection, or as a keyed hash only. Events never carry raw content.
- Every event carries a `CoverageClass` (GOVERNED / OBSERVED / BYPASS_DETECTED / UNOBSERVED) and a `Channel`.
- Types are immutable and reject unknown fields.

## Consequences
- `tests/harness/test_decision_contract.py` is the executable form of this ADR. Every implementation (the stub now, the real `CaptainCore` later) must pass it.
- Changing the contract after it is frozen requires a new ADR and a version bump of the HTTP API (`/core/v1` → `/core/v2`).
- Open points to settle before freezing: the HTTP transport shape for `/core/v1/*`, the streaming hook for output guardrails (Tier 2 chunk checks, CTO F3), and rate-limit reporting in `Limits`.
