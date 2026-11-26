# ADR-001: Captain Core first, as a modular monolith

- Status: Proposed
- Date: 2026-11-24
- Deciders: CTO, Solution Architect

## Context
Captain must govern AI usage arriving through seven integrations (API Gateway, IDE Connectors, Browser Extension, MCP Proxy, SDK Libraries, Hooks & Telemetry, Desktop Agent). If each integration implements identity, policy, budget or audit logic itself, rules will drift and evidence will be inconsistent. The CTO review (F9) also requires that gateway technology (LiteLLM) can be replaced without touching governance logic.

## Decision
1. **Captain Core is built first and is mandatory.** It contains eight modules: Identity & RBAC, Project Context, OPA Policy Engine, PII & Security Guardrails, Budget & Rate Limits, Approval Engine, Usage Collector, Evidence Ledger.
2. The backend is a **modular monolith** (one Python/FastAPI deployable, `captain.core.<module>` packages). PostgreSQL, Valkey, OPA, LiteLLM and guard models remain separate infrastructure.
3. Integrations live in `captain.integrations.*` (or separate clients such as extensions) and **may import only** `captain.core.decision_api` and `captain.core.models`. CI will enforce this with an import-linter rule in Phase 1.
4. **Gate:** no integration is merged until the Core passes its acceptance suite (`tests/harness/` plus the Phase 1 additions in IMPLEMENTATION_PLAN.md §5).

## Consequences
- Every integration's behaviour is the same by construction; adding one means writing a thin translator.
- The first two to three weeks produce no user-visible channel. The test harness and CLI are the demo.
- Module boundaries must be kept clean so modules can be split into services later if scale requires it.
