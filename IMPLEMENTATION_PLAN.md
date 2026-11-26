# Stixis Captain — Implementation Plan (v4: Captain Core first)

**Baseline: CTO Review V1.3 (23 Nov 2026) is the governing document.** Requirements v0.9 supplies scope (UC/FR), the Implementation Approach doc supplies structure, and the Claude Master Prompt supplies build rules. Where they conflict, the CTO review wins. Architecture follows the approved five-layer diagram (Sources → Integration Layer → Captain Core → Providers & Enterprise Systems → Dashboard). **Captain Core is built and accepted first.**

---

## 1. Decisions taken from the CTO review

| Area | CTO position (adopted) | Ref |
|---|---|---|
| Architecture | Approved: gateway, OPA, guardrails, tool proxy, ledger, live evaluation, register sync; tier (1–4) × autonomy (A0–A4) policy model is the core | R1 |
| Stack | Approved: LiteLLM OSS core, OPA, Presidio, Granite Guardian/NeMo, Python 3.12/FastAPI, React/TS, PostgreSQL, Valkey, OTel, Langfuse, Prometheus, Helm, OpenTofu | R2 |
| Gateway coupling | Policy and ledger logic sit **behind a Captain adapter, not inside LiteLLM callbacks**. Keys and budgets live in Captain tables. LiteLLM is pinned and every upgrade is reviewed. | F9, F13 |
| Endpoints | OpenAI-compatible endpoint **plus native pass-through per provider** | F10 |
| Team | 7-person shape confirmed: solution architect, platform engineer, 2 backend engineers, integration engineer, AI engineer, QA engineer. Front end: Rayudu + Claude. | R5 |
| Plan | **M1a core pilot by end of week 4 → M1b governance & agents in weeks 6–8 → M2 first client by week 12**, after the pen-test retest. Overall 8–12 weeks, tentative pending R&D. | R5, F1 |
| Acceptance | M1 acceptance covers **must-haves only**. Should-haves are "demonstrated if delivered". | F2 |
| Providers | Claude, OpenAI, Gemini in week 1. Azure OpenAI and Bedrock in week 2. Mistral and self-hosted in M1b. | D5 |
| Pilot | A low-risk Stixis workload that **includes a streamed workload** | D2 |
| Repo | Private GitHub, branch protection, required reviews, signed commits | D4 |
| Cloud | AWS vs Azure still open (to be confirmed from the client pipeline). Helm and OpenTofu stay portable. | D3 |

### NFRs adjusted by the CTO (R4, F12)
- Added latency p95 ≤ 50 ms without guardrails. ≤ 250 ms with guardrails for **non-streamed** calls. Streamed calls get per-tier targets for both added time to first token and added total time.
- 99.9% availability only with **≥2 guard-model replicas + an approved fallback**. Otherwise 99.9% covers Tier 3–4 and Tier 1–2 get a separate target.
- Tier 1–2 fail closed, including when evidence can't be durably queued. Tier 3–4 fail open with logging.
- New requirements to define:
  - RPO/RTO
  - retention default ≥ 6 months
  - crypto-shredding for erasure
  - false-block limits (e.g. ≤ 1% for Tier 1–2)
  - kill-switch / revocation within ≤ 5 s at p99
  - fleet ops (version pinning, release channels, content-free health monitoring)
  - targets for approval time, register-sync lag, alert delivery and ledger verify time

### Security controls required before the internal pilot (R3)
1. Ledger protection: per-entry KMS signing **and** hourly Merkle anchors to WORM.
2. Policy-bundle signing with named signers, the key in KMS, and **two-person approval for Tier 1 changes**.
3. Alert on any tier or autonomy downgrade, and require approval for it.
4. LiteLLM and gateway dependency pinning, with an upgrade review.
5. Break-glass procedure for Stixis operator access, with client notification.
6. Judge-model injection added to the threat model and test sets.
7. Corrected Rego with explicit deny reasons and an ordered `else` chain (F14).
8. External pen test, fix and **retest before M2**.

---

## 2. Architecture (as approved: five layers)

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 1. AI USAGE SOURCES                                                      │
│    IDE · Browser · SDK · Desktop · MCP · Agents                          │
└───────────────────────────────────┬──────────────────────────────────────┘
                                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 2. CAPTAIN INTEGRATION LAYER  (thin — translate & enforce, never decide) │
│    API Gateway          IDE Connectors        Browser Extension          │
│    MCP Proxy            SDK Libraries         Hooks & Telemetry          │
│    Desktop Agent                                                         │
└───────────────────────────────────┬──────────────────────────────────────┘
                                    ▼   Core Decision API
┌──────────────────────────────────────────────────────────────────────────┐
│ 3. CAPTAIN CORE — COMMON GOVERNANCE ENGINE   ★ built first, mandatory ★  │
│    • Identity and RBAC            • Budget and Rate Limits               │
│    • Project Context              • Approval Engine                      │
│    • OPA Policy Engine            • Usage Collector                      │
│    • PII and Security Guardrails  • Evidence Ledger                      │
└───────────────────────────────────┬──────────────────────────────────────┘
                                    ▼   (only approved calls)
┌──────────────────────────────────────────────────────────────────────────┐
│ 4. AI PROVIDERS & ENTERPRISE SYSTEMS                                     │
│    OpenAI · Anthropic · Gemini · GitHub · Jira · SQL · Internal APIs     │
│    (model calls via GatewayClient → LiteLLM; tool calls via MCP Proxy)   │
└───────────────────────────────────┬──────────────────────────────────────┘
                                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 5. CENTRAL CONTROL DASHBOARD                                             │
│    Employee · Project · Application · Agent · Usage · Cost · Violations  │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Captain Core: the eight modules

| # | Module | Responsibility | Key CTO conditions inside it |
|---|---|---|---|
| C1 | **Identity and RBAC** | Users, employees, service accounts, agents, virtual keys (hashed, rotate/revoke/expire), workload OIDC, SSO roles, **kill switch** state, revocation within 5 s | Key format `cpt_live_<id>_<secret>_<crc>`, F13 break-glass |
| C2 | **Project Context** | AI Register (tier, autonomy, owner, cost centre, purpose, allowed models/tools), projects, applications, environments, departments. Resolves every request to a context. Unknown context is never guessed. | Register is the source of truth (F8). Downgrades need approval. |
| C3 | **OPA Policy Engine** | Tier × autonomy + role/project/channel rules. Decisions: ALLOW, BLOCK, REQUIRE_APPROVAL, ALLOW_WITH_REDACTION, ALLOW_WITH_LIMITS, OBSERVE_ONLY, with reason code, policy version and trace ID | Signed bundles, two-person approval for T1, explicit deny reasons (F14), fail mode by tier |
| C4 | **PII and Security Guardrails** | Deterministic checks (secrets, banned terms, size), Presidio masking + restore, guard model for injection/safety, output checks, tool-argument inspection | 2 guard replicas + fallback (F5), false-block limits, streaming mode by tier (F3) |
| C5 | **Budget and Rate Limits** | Hierarchy org → cost centre → team → project → app → user → key → model/tool. Reserve → settle → release (Valkey Lua). RPM/TPM. 80/100% and burn-rate alerts. Audited override. | F6, counters rebuilt from PG |
| C6 | **Approval Engine** | Approval requests, approver assignment, pending tickets, timeout = refuse, delegation, argument-hash binding, single use, re-check at release | F7. Channel-agnostic: used by the MCP Proxy, API Gateway and Browser |
| C7 | **Usage Collector** | Normalized usage (reported vs estimated vs observed), versioned pricing (micro-units), cost records, coverage class GOVERNED / OBSERVED / BYPASS_DETECTED / UNOBSERVED, reconciliation and chargeback | F6 discounts / caching |
| C8 | **Evidence Ledger** | Outbox → ordered writer, RFC 8785 + SHA-256 chain, KMS signing, hourly Merkle anchor to WORM, append-only DB, `verify` CLI, HMAC payload hashes (crypto-shredding), export | F4, F12 |

### 2.2 Core Decision API (the only door into the Core)

Every integration calls the same five operations, exposed in-process (Python package) and over HTTP (`/core/v1/*`) for out-of-process integrations such as extensions, the desktop agent and telemetry hooks:

| Operation | Purpose |
|---|---|
| `resolve(credential, hints)` | Identity + project context |
| `decide(GovernanceRequest)` | Policy + guardrails → structured decision |
| `reserve(decision, estimate)` / `settle(reservation, actual_usage)` | Budget |
| `request_approval(...)` / `approval_status(ticket)` | Approval Engine |
| `record(event)` | Usage Collector + Evidence Ledger (also accepts OBSERVED events from telemetry) |

`GovernanceRequest` carries: principal, context, `channel` ∈ {API, IDE, BROWSER, MCP, SDK, HOOK, DESKTOP}, action (model call / tool call / web submit), model or tool, content or content hash, and coverage class.

### 2.3 Integration Layer: seven integrations

| Integration | What it does | Coverage |
|---|---|---|
| **API Gateway** | OpenAI-compatible + native passthrough endpoints for apps; calls Core, then LiteLLM | GOVERNED |
| **SDK Libraries** | Thin Python/.NET/JS wrappers: set the Captain endpoint, pass project/retrieval context, handle approval-pending | GOVERNED (via API Gateway) |
| **MCP Proxy** | MCP server to agents, MCP client to GitHub/Jira/SQL/internal APIs; tool allowlists and argument checks via Core | GOVERNED |
| **IDE Connectors** | VS Code companion + custom-endpoint config for coding tools; vendor telemetry import | GOVERNED where an endpoint can be set, otherwise OBSERVED |
| **Hooks & Telemetry** | Ingest vendor audit/usage APIs (Copilot, Cursor, ChatGPT Enterprise, Claude for Work), coding-agent hooks, IdP sign-ins, proxy/CASB logs for discovery | OBSERVED / BYPASS_DETECTED |
| **Browser Extension** | MV3 extension, site adapters, pre-submit check against Core | UI-level GOVERNED / OBSERVED |
| **Desktop Agent** | Managed endpoint agent for desktop AI apps (detect, report, route config); feasibility study first | OBSERVED (enforcement only where OS/vendor allows) |

Rule: no integration contains policy, budget, identity or audit logic. If an integration needs a new capability, it is added to the Core first.

### 2.4 Code layout
```
backend/captain/
  core/                         ← built first
    identity/  context/  policy/  guardrails/  budget/  approvals/  usage/  ledger/
    decision_api.py   models.py (GovernanceRequest, Decision, UsageEvent)
  integrations/  api_gateway/  mcp_proxy/  ide/  telemetry/  browser/  desktop/
  gateway/       client.py · litellm.py · mock.py
  admin/         dashboard APIs
  workers/       ledger_writer · anchor · reconciliation · alerts · register_sync
sdk/             python/ · dotnet/ · js/
extensions/      vscode/ · browser/
desktop-agent/
console/         Central Control Dashboard
policies/        rego + tests
```

### 2.5 Fail modes (CTO-confirmed)

| Down | Tier 1–2 | Tier 3–4 |
|---|---|---|
| OPA / guard model | Fail closed | Fail open + log |
| Valkey | Fail closed during rebuild | Fail open, soft limits |
| Ledger outbox | Refuse | Refuse |
| Provider | Policy-allowed fallback, otherwise error | Same |

---

## 3. Pre-build pack (week 0)

| # | Item | Owner |
|---|---|---|
| P1–P2 | v1.0 sign-off, decisions D1–D5 | CEO / CTO |
| P3 | ADRs: Core-first modular monolith; Core Decision API contract; ledger (F4); budget reservation (F6); streaming (F3); Register SoT (F8); gateway adapter + passthrough (F9/F10); logging by tier | Claude → architect |
| P4 | Core data model + OpenAPI for Core Decision API and admin API | Claude → architect |
| P5 | Threat model (STRIDE, OWASP LLM Top 10, F13 items) | Claude → architect |
| P6 | Tier × autonomy matrix + Rego + `opa test` | Claude → AI Architect |
| P7 | Monorepo, Compose, CI, mock provider | Claude |
| P8 | Dashboard design system | Rayudu + Claude |
| P9 | Test plan + golden sets (PII, injection, jailbreak, benign, judge injection) | Claude → QA |
| P10 | Repo, dev cluster, vault, identity tenant, provider test keys | Engineering |
| P11 | Pilot workload with streaming | CEO |
| + | Requirements traceability matrix; pilot metrics list | Claude |

---

## 4. Delivery strategy: Captain Core first

```
 Phase 1  CAPTAIN CORE (mandatory)      wk 1–3   all 8 modules + Core Decision API, tested with a harness, no integration
 Phase 2  MVP: first integration        wk 3–4   API Gateway + SDK (Python) + basic Dashboard → internal pilot (M1a)
 Phase 3  Enterprise E1                 wk 5–8   MCP Proxy, IDE Connectors, Hooks & Telemetry, full Dashboard (M1b)
 Phase 4  Enterprise E2                 wk 9–12  First client deployment, pen test + retest (M2)
 Phase 5  Enterprise E3                 wk 13+   Browser Extension, Desktop Agent, more SDKs, fleet ops
```

**Gate rule:** no integration work starts until the Core Decision API contract is frozen (end of week 1). No integration merges until the Core passes its Phase 1 acceptance suite.

---

## 5. Phase 1: Captain Core (weeks 1–3, mandatory)

The Core is built and proven **on its own**, driven by a test harness and a `captain-core` CLI that sends `GovernanceRequest`s, before any real integration exists.

| Week | Platform | Backend 1 | Backend 2 | Integration | AI eng | Frontend | QA |
|---|---|---|---|---|---|---|---|
| 1 | C1 Identity: keys, hashing, auth, workload OIDC; revocation pub/sub | C3 Policy: OPA input/output contract, tier rules, reason codes; **Decision API contract frozen** | C5 Budget: hierarchy, reserve/settle Lua, rate limits | C2 Context: local Register, projects/apps/envs, resolver | C4: deterministic checks, Presidio | Dashboard shell, SSO login | Harness, unit test plan |
| 2 | C1: SSO/RBAC, kill switch | C8 Ledger: outbox, writer, chain, KMS signing, verify | C7 Usage: normalization, pricing versions, cost records | C2: downgrade approval, Register adapter interface | C4: guard model ×2 + fallback, masking/restore | Admin pages for keys, register, budgets | Policy + guardrail tests |
| 3 | Decision API over HTTP, OTel | C8: hourly anchor, append-only roles; C3 bundle signing + 2-person T1 | C6 Approval Engine (channel-agnostic) | C7: reconciliation import, chargeback | C4: output checks, streaming-chunk checker, false-block rate | Usage, ledger verify page | Core acceptance suite, budget race, tamper tests |

**Core acceptance (exit gate, end of week 3)** — all via harness:
- Every decision type is produced with the correct reason code. No "no matching rule" outcome.
- Tier 1–2 fail closed and Tier 3–4 fail open when OPA, guard model or Valkey are down.
- 100 concurrent reservations on a nearly exhausted budget: no overshoot.
- Revocation and kill switch take effect within 5 s at p99.
- PII masked and injection blocked/logged by tier; detection and false-block rates recorded.
- Approval: pending → approve/reject/timeout; replay refused; re-check at release.
- Ledger verify passes, fails at the right entry after tamper; anchors written.
- Core decision latency p95 ≤ 50 ms without guardrails (in-process).
- Every Core action emits a ledger event.

---

## 6. Phase 2: MVP (weeks 3–4 → M1a internal pilot)

First integration on top of the finished Core:
- **API Gateway:** `/v1/chat/completions` (streaming per tier), `/v1/models`, `/v1/usage`, `/v1/governance/status`, `/health`, Anthropic native passthrough. Size limits, timeouts, idempotency keys, safe retries. `GatewayClient` → pinned LiteLLM → Claude, OpenAI, Gemini, Azure OpenAI, Bedrock.
- **SDK Library (Python):** thin wrapper; passes project and retrieval context.
- **Dashboard (basic):** Employee, Project, Application, Usage, Cost and Violations views; keys; budgets; request detail with decision explanation; ledger verify.
- **Ops:** Helm to the pilot cluster, egress NetworkPolicy, runbooks, break-glass.

Not in MVP: MCP Proxy, IDE Connectors, Hooks & Telemetry, Browser, Desktop, Register sync, ServiceNow, live evaluation, policy editor UI. The Approval Engine exists in Core but has no user-facing channel until E1.

**MVP acceptance (M1a):**
- 100% of pilot calls go through Captain, with no developer-held provider keys (secret scan + egress test).
- Cap refusal with a clear reason, plus the 80% alert.
- The streamed workload follows its tier rules, with time to first token measured.
- Chargeback is produced automatically.
- SSO and roles restrict access.
- Latency is within target at pilot load.
- Pilot metrics are reported.

---

## 7. Enterprise application

### E1 — weeks 5–8 (M1b)

| Integration / area | Deliverables |
|---|---|
| **MCP Proxy** | Streamable HTTP (verify current spec), tool registry + JSON Schema, per-agent allowlist, Approval Engine wiring, GitHub or Jira + SQL read-only example, kill switch per agent/server |
| **IDE Connectors** | VS Code companion, custom-endpoint setup (Claude Code, Codex, Continue) |
| **Hooks & Telemetry** | Copilot / Cursor / ChatGPT Enterprise / Claude for Work usage import (OBSERVED), IdP + proxy discovery (BYPASS_DETECTED), ServiceNow incidents |
| **Core extensions** | Register sync (SoT), live evaluation (in-perimeter judge for T1–2), evidence export by control (NIST / ISO 42001 / EU AI Act), retention + crypto-shredding |
| **Dashboard** | Agent view, MCP governance, approvals & alerts, policy lifecycle UI (templates → simulate → approve → publish → rollback), coverage view, audit explorer, CSV export |

### E2 — weeks 9–12 (M2 first client)
- OpenTofu for the chosen cloud.
- Per-client install with the client's IdP and vault.
- Backup/restore drill with ledger re-verify.
- Pen test, fixes, **retest** before go-live.
- Client policies, UAT, handover.

### E3 — week 13+
- **Browser Extension:** MV3, ChatGPT/Claude/Gemini site adapters, pre-submit checks, managed deployment guide.
- **Desktop Agent:** feasibility study, then an observe-first agent for desktop AI apps.
- **SDK Libraries:** .NET and JS.
- More API types: Responses, Gemini native, embeddings, media.
- Fleet ops and a second cloud.
- SOC 2 track.
- ServiceNow app decision at M4.

### Roadmap at a glance

| Layer / item | Phase 1 Core | MVP | E1 | E2 | E3 |
|---|---|---|---|---|---|
| Captain Core (8 modules) | ✅ | | Register sync, evaluation, export | Client tuning | ABAC, predictive spend |
| API Gateway | | ✅ | | | More APIs |
| SDK Libraries | | Python | | | .NET, JS |
| MCP Proxy | | | ✅ | | |
| IDE Connectors | | | ✅ | | v2 |
| Hooks & Telemetry | | | ✅ | | More vendors |
| Browser Extension | | | | | ✅ |
| Desktop Agent | | | | | ✅ |
| Dashboard | Admin basics | Basic views | Full governance | Client roles | Exec reporting |
| Deployment | Compose | Pilot Helm | | Per-client + pen test | Fleet, 2nd cloud |

---

## 8. Open items
1. Team names and kick-off date (D1).
2. Pilot workload with streaming (D2).
3. AWS vs Azure (D3).
4. Guard model hosting (D6).
5. Does an AI Register API exist?
6. GitHub or Jira (plus which SQL source) for MCP.
7. Desktop Agent: which desktop AI apps are in scope, and is endpoint management (Intune/Jamf) available at clients?
8. SDK languages after Python.
9. Code-review capacity for Claude-drafted code.

## 9. Verification against the Master Prompt and the CTO review
- **Kept:**
  - Core is shared and adapters stay thin.
  - Modular monolith, with LiteLLM behind an interface.
  - Six decision types with a structured result.
  - Metadata-only by default, with an honest coverage model.
  - Phase acceptance criteria, ADRs and a traceability matrix.
  - Tests use mock providers only.
- **Changed per your architecture:**
  - Four adapters become seven integrations (SDK Libraries, Hooks & Telemetry and Desktop Agent added).
  - The Approval Engine is a Core module.
  - The Core is built and accepted before any integration.
- **Changed per the CTO review:**
  - 7-person team with the M1a / M1b / M2 milestones.
  - Guardrails, SSO and the signed ledger are in the first release.
  - Helm deployment per client.
  - Pen-test retest before go-live.

## 10. Recommended first task
**Week 0–1: Core skeleton + Core Decision API contract.**
- Monorepo and Docker Compose (Postgres, Valkey, OPA, mock provider).
- `core/models.py` with `GovernanceRequest`, `Decision`, `UsageEvent` and `LedgerEvent`.
- `decision_api.py` interface with stub implementations.
- Test harness and CI.
- ADR-001 (Core-first modular monolith) and ADR-002 (Core Decision API contract).

Every Core module and every integration builds against this contract.
