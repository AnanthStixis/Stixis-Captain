# Stixis Captain

Enterprise AI governance: **AI Usage Sources → Captain Integration Layer → Captain Core → AI Providers & Enterprise Systems → Central Control Dashboard.**

Status: Phase 1 in progress. Captain Core is implemented and tested: all eight modules, the `CaptainCore` engine, PostgreSQL persistence (including budget counters and the event bus), workload OIDC sign-in, KMS-ready ledger signers, and signed OPA policy bundles. End-to-end tests run the Core against a real OPA server and local PostgreSQL with no test doubles. OPA binary for local runs: `tools/opa/opa.exe` (v1.0.0, gitignored; or set `CAPTAIN_OPA_BIN`). Run policy tests with `tools\opa\opa.exe test policies`. Presidio NER detection is wired but optional (`pip install -e ".[presidio]"` + a spaCy model).

### PostgreSQL integration tests
Put `CAPTAIN_TEST_DATABASE_URL=postgresql+psycopg://captain_test:<password>@localhost:5432/captain_test` in `backend/.env` (gitignored; see `backend/.env.example`). The suite drops and recreates the schema on each run, so use a dedicated test database.

## Layout
```
backend/captain/core/     Captain Core (built first) — models.py, decision_api.py, stub.py, 8 module packages
backend/tests/harness/    Core Decision API contract tests (any Core implementation must pass)
policies/                 OPA Rego bundles
docs/adr/                 Architecture decision records
Ref_Doc/                  Source requirements and CTO review
```

## Run locally (Windows PowerShell)
```powershell
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
```

## Run the Core services locally
One-time setup (from `backend`):
```powershell
.venv\Scripts\python -m captain.devsetup
```
This writes dev settings and secrets to `backend\.env` and `backend\.dev\` (both gitignored), and writes the SQL for a `captain_dev` database to `backend\.dev\create_captain_dev.sql`. Run that SQL once as the `postgres` superuser, then delete the file:
```powershell
& 'C:\Program Files\PostgreSQL\18\bin\psql.exe' -U postgres -h localhost -d postgres -f .dev\create_captain_dev.sql
```
```powershell
.venv\Scripts\python -m alembic upgrade head
```
```powershell
.venv\Scripts\python -m captain.devseed
```
`devseed` creates a demo application (`AI-DEV`) and saves its key to `backend\.dev\dev-app.key`.

Run each service in its own terminal:
```powershell
& ..\tools\opa\opa.exe run --server --addr=127.0.0.1:8181 ..\policies
```
```powershell
.venv\Scripts\python -m captain.workers
```
```powershell
.venv\Scripts\python -m uvicorn --factory captain.api.core_http:create_app_from_env --host 127.0.0.1 --port 8080
```
The worker writes the evidence ledger, anchors it hourly, releases stale budget reservations, escalates overdue approvals and retries alert delivery; only one worker holds the PostgreSQL lease. The API serves `/core/v1/*` for out-of-process integrations.

Note: start OPA from the repo root with a relative `policies` path if `..\policies` is rejected. On Windows OPA reads `C:\...` as `<prefix>:<path>`.

## API Gateway (Phase 2)
Applications use the standard OpenAI SDK and change only the base URL and key:
```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key=open(".dev/dev-app.key").read().strip())
client.chat.completions.create(model="claude-sonnet", messages=[{"role": "user", "content": "hi"}])
```
Run locally (in addition to OPA and the worker), each in its own terminal from `backend`:
```powershell
.venv\Scripts\python.exe -m uvicorn captain.devprovider:app --host 127.0.0.1 --port 8090
```
```powershell
.venv\Scripts\python.exe -m uvicorn --factory captain.api.gateway_main:create_gateway_app_from_env --host 127.0.0.1 --port 8000
```
`captain.devprovider` is a deterministic mock model (no real spend). For real providers, register a provider whose `base_url` points at a LiteLLM proxy (or OpenAI directly) with `credential_ref=env:<VAR>`.

## Central Control Dashboard (Phase 2)
Build once, then the Admin API serves the dashboard and the API from one address:
```powershell
cd console; npm ci; npm run build; cd ..\backend
```
```powershell
.venv\Scripts\python.exe -m uvicorn --factory captain.api.admin_main:create_admin_app_from_env --host 127.0.0.1 --port 8100
```
Open http://127.0.0.1:8100 and sign in with `CAPTAIN_DEV_ADMIN_TOKEN` from `backend\.env` (accepted from this machine only). For UI development with hot reload run `npm run dev` in `console` (port 5173, proxies `/admin` to 8100). Production sign-in is SSO: set `CAPTAIN_ADMIN_OIDC` (issuer, audience, JWKS, group-to-role mapping).

### Single sign-on (browser)
The console signs users in with OpenID Connect (authorization code + PKCE). Captain does the token exchange on the server; the browser only holds an HttpOnly session cookie, and changes need a per-session CSRF token. Local development uses a mock identity provider with five fictional users (admin, approver, FinOps, auditor, and one with no Captain role):
```powershell
.venv\Scripts\python -m captain.devsetup
```
```powershell
.venv\Scripts\python.exe -m uvicorn --factory captain.devidp:create_dev_idp_from_env --host 127.0.0.1 --port 8200
```
Then restart the Admin API (port 8100) and click **Sign in with SSO**. For a real identity provider (Entra ID, Okta) set `CAPTAIN_ADMIN_OIDC` (issuer, client id and secret, the HTTPS redirect URI `https://<host>/admin/v1/auth/callback`, and the group-to-role mapping) and `CAPTAIN_SESSION_SECRET`; register that redirect URI at the identity provider. See `backend/captain/api/admin_main.py` for every setting. The mock identity provider must never be exposed beyond this machine.

## MCP Proxy (Phase 3)
Agents point their MCP client at Captain instead of the tool servers. Captain shows each agent only the tools its AI Register entry allows, decides every `tools/call` (kill switch, registry, guardrails, risk, OPA) before the tool server is contacted, holds high-impact calls for human approval, screens results as untrusted content, and records evidence. One endpoint, `POST /mcp`, serves both protocol eras (2026-07-28 stateless, and the 2025 `initialize` handshake) and was checked against the official MCP Python SDK in both.

Local run (in addition to OPA, the worker and the Admin API; each in its own terminal from `backend`; run `python -m captain.devsetup` and `python -m captain.devseed` again to add the demo agent and key `.dev\dev-agent.key`):
```powershell
.venv\Scripts\python.exe -m uvicorn --factory captain.devmcp:create_dev_mcp_from_env --host 127.0.0.1 --port 8310
```
```powershell
.venv\Scripts\python.exe -m uvicorn --factory captain.api.mcp_main:create_mcp_app_from_env --host 127.0.0.1 --port 8300
```
`captain.devmcp` is a mock tool server (set `CAPTAIN_MOCK_MCP_MODE=legacy` to behave like a 2025-era server). The demo agent (`AI-AGENT`, Tier 3, autonomy A3) may call `demo.search_issues` freely; `demo.create_issue` is held. A held call returns a tool result naming an approval ticket; after an approver decides it in the dashboard (Approvals page, signed in as Approver One), the agent repeats the same call with `_meta["com.stixis.captain/approval_ticket"]` set to the ticket. The approval is bound to the caller, the tool and the exact arguments, and works once. To try it with the official SDK see `backend/scripts/mcp_sdk_check.py`.

Known limits: tools have no money budget (they cost nothing to run), but per-tool RATE limits exist (see below); only text tool output is passed on (images and other content are removed with a note); Tier 3-4 results are passed through and findings only recorded; no batches, server push streams, sampling or elicitation; the upstream connection IS pinned to the address that was validated (DNS rebinding closed; still restrict egress to the registered MCP hosts as a second layer); the 2026-07-28 wire format is verified against the official SDK client, not yet against third-party servers.


## Anthropic Messages endpoint (Claude Code and Anthropic SDKs)
The gateway also serves `POST /v1/messages` (streaming and not), `POST /v1/messages/count_tokens` (approximate) and `GET /v1/models`, so tools that speak the Anthropic wire format can use Captain. It runs the same governed pipeline as `/v1/chat/completions` (channel `IDE`): kill switch, registry, guardrails, policy, budget, output guard, evidence. The model behind each alias is reached through the registered OpenAI-compatible provider (for real models, a LiteLLM proxy translates onwards).

Point a client at it with its base URL (no `/v1`) and a Captain key, in `x-api-key` or `Authorization: Bearer`. Claude Code documents `ANTHROPIC_BASE_URL` plus `ANTHROPIC_AUTH_TOKEN` for gateways; the model name it sends must be a registered Captain alias. Try it with the official SDK: `backend/scripts/try_messages.py` (install `anthropic` in a separate environment).

Supported: text, system prompts, tools, tool calls and results, streaming, stop sequences. Refused with a clear 400 rather than dropped: images, documents, thinking blocks and server tools. Hints such as `thinking`, `output_config`, `metadata` and `cache_control` are accepted and ignored. Limits: the model's own tool calls are passed through but not inspected, and tools that Claude Code runs on the developer's machine are not governed by Captain (only the model calls are); no `/api/hello` probe; not yet tried with Claude Code itself, only the Anthropic SDK and unit tests.

## OpenAI Responses endpoint (Codex and recent OpenAI SDKs)
`POST /v1/responses` (streaming and not) runs the same governed pipeline as the other gateway routes (channel `IDE`), so Codex with `wire_api = "responses"` and the OpenAI SDK's `client.responses` can use Captain. For Codex, define a model provider whose `base_url` is `http://<gateway>/v1` and whose `env_key` names the variable holding a Captain key, and select a registered Captain alias as the model; `env_key` holds a variable name, never the key itself. Try it with the official SDK: `backend/scripts/try_responses.py`.

Supported: text, instructions, function tools, function calls and their outputs, streaming. Stateless: Captain stores no responses, so `previous_response_id` is refused (send the full input) and `store` is ignored. Refused with 400 rather than dropped: images and files, reasoning items, built-in or custom (freeform) tools, structured output formats. Not yet tried with Codex itself, only the OpenAI SDK and unit tests; Codex versions that send custom tools such as a freeform patch tool will get a 400 until those are mapped. The wire format follows the OpenAI SDK's types, because the reference pages could not be fetched.

## VS Code companion (`ide/vscode`)
A small extension that tells a developer whether their editor is connected to Captain and helps set up governed endpoints. The status bar shows the AI Register entry and tier the key belongs to (or "not signed in", "key rejected", "gateway unreachable"), and the tooltip says plainly that it covers calls made with that key, not other AI tools on the machine. Commands: **Captain: Set Key** (kept in the editor's secret storage, sent only as a bearer header, never to a non-https gateway except this machine), **Clear Key**, **Refresh Status**, **Show Status**, and **Open Continue Configuration** (opens the generated config for review; it never writes over your own Continue settings). Setting: `captain.gatewayUrl` (default `http://127.0.0.1:8000`).
```powershell
cd ide\vscode; npm ci; npm test; npm run package
```
Install the resulting `captain-vscode-0.1.0.vsix` with **Extensions: Install from VSIX...** in VS Code. The logic is unit-tested and the wiring is tested against a fake `vscode` module, and the client code was run against a live gateway; the extension has not been run inside VS Code itself. It does not detect other AI tools, enforce anything locally, or sign users in with SSO yet.

## Claude Code hook (`ide/claude-code/captain_hook.py`)
A single standard-library Python file that Claude Code runs for hook events and that talks to the Captain Core API (`/core/v1/*`, port 8080 locally). Print the settings to install with `python ide/claude-code/captain_hook.py --print-settings` (add `--strict` to make Claude Code block when the hook itself fails) and put the result in `~/.claude/settings.json`, a project's `.claude/settings.json`, or the organisation's managed settings. Give it a Captain key with `CAPTAIN_KEY` or a key file (`~/.captain/key`), and the Core URL with `CAPTAIN_CORE_URL`.

- **PreToolUse (governed before it runs):** each tool call is decided by the Core as tool `claudecode.<ToolName>`, so tools must be registered in the Model/Tool Registry and allowed for the developer's AI Register entry; anything else is refused (`devseed` registers Read, Glob, Grep, Edit, Write, Bash for the demo developer, whose key is `.dev\dev-coder.key`). A held call returns a deny that names the approval ticket; once an approver decides it in the dashboard, the same call, retried, runs once. A second identical call needs a new approval.
- **PostToolUse, UserPromptSubmit, SessionStart (observed):** recorded as `OBSERVED` evidence. Only the tool name, duration and prompt length are sent; never prompts, commands or tool output.
- **Failure behaviour:** if Captain cannot be reached the action proceeds (Claude Code's own default) unless `CAPTAIN_HOOK_FAIL_CLOSED=1`. A rejected key blocks.

Honest coverage: enforcement exists only while the hook is installed and running. A developer can remove a user-level hook; use managed settings with `allowManagedHooksOnly` to prevent that. Observation records that something happened and cannot stop it. Not yet done: vendor usage importers (Copilot, Cursor, ChatGPT Enterprise, Claude for Work), IdP and proxy discovery, prompt content checks, and hooks for other coding tools. Built against the Claude Code hooks reference (checked 2026-11) and run against the live Core API; not yet run from inside Claude Code itself.

## Vendor usage importers (`captain.integrations.telemetry`)
Usage that happens outside Captain can be imported as `OBSERVED` evidence: the vendor says it happened, Captain did not see it first and could not have stopped it. Observed records never touch a budget and are not mixed into governed spend. Importing a period twice is safe (event ids come from the vendor and the record's own id; the ledger ignores what it has already sealed). Vendor identities are mapped to Captain principals with `--actor-map` (JSON, identity to principal id); an identity that is not in the map is recorded as a short hash, never as the raw email or login.
```powershell
$env:ANTHROPIC_ADMIN_KEY = "<admin key>"; .venv\Scripts\python -m captain.integrations.telemetry claude-code --day 2026-10-19 --actor-map actors.json
```
```powershell
$env:GITHUB_TOKEN = "<token>"; .venv\Scripts\python -m captain.integrations.telemetry copilot --org <org> --day 2026-10-19
```
```powershell
.venv\Scripts\python -m captain.integrations.telemetry csv --file export.csv --mapping mapping.json
```
Run it from `backend` with the ledger worker running (it seals the events). Credentials come from the environment, never from arguments.

- **Claude Code** (Anthropic Admin API `usage_report/claude_code`): per-user daily sessions, lines, tool accept counts, and per-model tokens and estimated cost (the API reports cents; stored as Captain money units). Covers Console-organisation usage only, not Claude subscription seats; data lags up to an hour.
- **GitHub Copilot** (Copilot usage metrics `users-1-day` report): per-user daily activity counts. Copilot reports counts, not tokens or cost. The report file format behind GitHub's download links is not documented, so the reader accepts a JSON array, an object holding a list, or JSON lines and refuses anything else; check it against a real report first. The GitHub token is never sent to the signed download links.
- **CSV** (Cursor, ChatGPT Enterprise, anything else): no vendor export format is assumed; an administrator supplies the column mapping (see the module docstring in `csv_import.py`).

Built from the vendors' documentation and tested with responses shaped like it; no live vendor account was used. Not done: scheduling, Cursor and ChatGPT Enterprise API adapters, Claude for Work analytics, the Anthropic Messages usage report (per key and workspace), and showing observed usage on the dashboard beyond the existing observed-event count.

### Observed usage on the dashboard
The console has an **Observed usage** page (permission `usage:read`) backed by `GET /admin/v1/observed?days=`. It shows vendor imports and coding-tool hook reports separately from governed activity: events per day, a per-source table (how it arrives, events, people or keys, tokens, vendor-reported cost, days covered) and the top people and keys, with identities that are not in the actor map marked "not matched" and shown only as a short hash. Vendor-reported cost is never added to governed spend; governed spend for the same period is shown beside it for comparison and the page says the two may overlap. Vendor imports are placed by the usage day they report, hook events by when they were recorded.

### Scheduled imports
The Core worker can run vendor imports for you, on its own thread so a slow vendor never delays the ledger writer. Configure them in the worker's environment (`backend\.env` for local development):
```
CAPTAIN_IMPORTS=[{"source":"claude-code","every_hours":6,"lookback_days":3},{"source":"copilot","org":"acme"}]
ANTHROPIC_ADMIN_KEY=...        # credentials stay out of CAPTAIN_IMPORTS
GITHUB_TOKEN=...
CAPTAIN_ACTOR_MAP_FILE=C:\path\to\actors.json
```
Each run looks at the last `lookback_days` complete days (a day is complete `settle_hours`, default 3, after midnight UTC, because vendor data lags) and imports each day once: a day that imported cleanly is remembered in PostgreSQL, survives restarts, and is never fetched again; a day that failed is retried on the next run. Three failed runs in a row raise one alert, and a success clears it. A job whose settings are wrong (missing credential, unknown source, missing org) is logged at startup and skipped; the rest of the worker keeps running. Only the worker that holds the lease runs imports.

A completed day is treated as final: if a vendor later revises that day's numbers, or a record appears late, it is not picked up automatically. Run `python -m captain.integrations.telemetry ...` by hand for that day; records already sealed stay as they were. Copilot's report file format is still unverified (see above). For local development `captain.devvendor` is a mock of the Claude Code analytics API (`uvicorn captain.devvendor:app --port 8320`; add `"base_url":"http://127.0.0.1:8320"` to the job).

### Real identity provider
For Microsoft Entra ID, Okta or another OpenID Connect provider follow `docs/sso-setup.md`, then run `python -m captain.sso_check` (checks the settings, the provider's discovery document, signing keys and role mapping without a person signing in). Captain recognises Entra's group overage, chooses the token-endpoint authentication method the provider supports, checks the authorized party, and matches group ids case-insensitively. Not yet run against a real tenant.

### What was masked, in the evidence
Decision events (`call.masked`, `call.allowed`, `call.refused`, ...) now record what the guardrails found as kinds and counts only, for example `PII:EMAIL_ADDRESS x2, SECRET:AWS_ACCESS_KEY x1` (`details.detected`), never the matched text or where it was. The Activity page shows it under the reason ("found: ...") and in the request detail drawer. Guard-model scores close to zero are not listed as findings. Events recorded before this change have no such field.

## Python SDK (`sdk/python`)
A thin typed client for the gateway (`pip install ./sdk/python`, depends only on httpx).
```python
from captain_sdk import CaptainClient, BudgetExceededError, RefusedError

captain = CaptainClient(api_key="cpt_live_...", base_url="https://captain.example.com")
try:
    r = captain.chat("claude-sonnet", [{"role": "user", "content": "Hello"}], max_tokens=200)
    print(r.text, r.decision, r.usage.total_tokens, r.request_id)
except BudgetExceededError as e:
    print("stopped by a budget:", e.code)       # reason codes are on every refusal
except RefusedError as e:
    print("refused:", e.code, e)

for piece in captain.stream("claude-sonnet", [{"role": "user", "content": "Hi"}]):
    print(piece, end="")
print(captain.usage().tightest_remaining_usd, captain.status().risk_tier)
```
It adds typed errors that carry Captain's reason code, the governance facts of each call (decision, risk, request id), an `Idempotency-Key` on every call, and `status()`, `usage()` and `models()`. It refuses to send the key over plain http to another machine. It does not retry model calls (the gateway claims an idempotency key before calling the provider, so a repeat is answered "already used"); only a failed connection, where nothing reached Captain, is retried. Marking retrieved documents as untrusted content is not in the SDK yet.

## Usage endpoints and views
- `GET /v1/usage` (with a Captain key): how much of the caller's budgets is used this period, in micro-units (1 USD = 1,000,000). It lists the caller's own budgets (person, key, application, project) and a `tightest` figure across all budgets that apply, naming only the kind of budget for broader ones (organisation, cost centre, team).
- **Usage by...** page in the console (`GET /admin/v1/usage/breakdown?by=principal|project|app|cost_centre|team|model`): governed spend, calls, tokens and share of spend per person, project, application, cost centre, team or model; refusals are counted per person.

## Gateway load test
With the services running (use the mock model), from `backend`:
```powershell
.venv\Scripts\python -m bench.gateway_load --requests 300 --concurrency 10
```
```powershell
.venv\Scripts\python -m bench.gateway_load --stream --requests 100 --concurrency 5
```
It prints p50 / p95 / p99 latency, time to first token for streams, throughput and outcomes by HTTP status. With the mock model this measures Captain's own overhead on the real stack; it is not a measurement of a real provider.

## Operations (pilot cluster)

Files for the pilot cluster; **none of it has been built, linted or deployed** (no Docker in this development flow, no Helm or OpenTofu installed). Treat it as a reviewed starting point. Azure: see "Azure deployment (E2)" below.

- `backend/Dockerfile`: one image for every process.
- `deploy/helm/captain`: gateway, Core API, Admin API and the ledger worker (one replica, never two writers). Secrets come from an existing Kubernetes Secret you create; nothing secret is in `values.yaml`. Values marked `CHANGE` must be set.
- `deploy/helm/captain/templates/networkpolicy.yaml`: default deny, then only what each process needs. Only the gateway may reach model providers, which is what makes "no developer-held provider keys" enforceable. Narrow `providerEgress` to your providers or egress proxy.
- `docs/runbooks.md`: leaked key, kill switch, budgets, provider outage, Captain unhealthy, ledger verify, restore.
- `docs/break-glass.md`: owner-role access, with approval, time limit, client notification and review. Two steps are marked as decisions for you.

SDK: `captain.chat(model, messages, retrieved=[...])` adds retrieved documents as fenced, "untrusted data" context. It is a client convention; the gateway's own injection scan is the real control.

## Governance views, exports and discovery

- **Coverage** page (`/admin/v1/coverage`): governed vs observed-only vs bypass detected vs unobserved, by channel and person.
- **Agents & systems** page (`/admin/v1/agents`): each register entry with allowed tools and what it actually did.
- **CSV export**: Download CSV on Activity and Usage by… (`/admin/v1/events.csv`, `/admin/v1/usage/breakdown.csv`); cells that could run as spreadsheet formulas are neutralised.
- **Security events**: refusals that cannot be tied to a register entry (wrong or revoked key, retired system) are recorded in the ledger partition `security`, once per reason and source per 30 s, never with the credential.
- **Bypass discovery**: `python -m captain.integrations.telemetry discovery --file proxy.csv --mapping map.json` turns a web-proxy, DNS or IdP log export into `BYPASS_DETECTED` signals (never spend, never governed). See the module docstring for the mapping; the AI-service list is a starting point you extend.
- **ServiceNow**: set `CAPTAIN_SERVICENOW_URL`, `_USER`, `_PASSWORD` (and optionally `_MIN_SEVERITY`, `_GROUP`) and alerts at or above the severity open incidents. Tested against a fake instance only.
- **Register sync**: `python -m captain.register_sync --file register.json` (dry run) / `--apply`. Weakening changes need `--approval-ref`; entries missing from the file are never retired.

## JavaScript / TypeScript SDK

`sdk/js` (`@stixis/captain-sdk`, Node 20+, no runtime dependencies): `new CaptainClient({apiKey, baseUrl})` with `chat`, `stream` (async iterable), `models`, `status`, `usage`, typed errors that carry Captain's reason code, an idempotency key on every call, `retrieved: [...]` fenced as untrusted data, and the same rules as the Python SDK (model calls are never retried; only a failed connection is; plain http only to this machine). `cd sdk/js; npm install; npm test`. Run live against the gateway and the mock model.

`POST /v1/embeddings` (OpenAI-compatible; texts only, up to 256 inputs): governed like a chat call. The text is inspected and masked BEFORE it reaches the provider, the budget is reserved and settled on input tokens, and the call is recorded. Token-id inputs are refused (they cannot be inspected). Both SDKs have `embed()`. Needs an embedding model registered with a price (output price 0). Tested against the mock provider only.

**Restore drill:** `backend/scripts/restore_drill.py` dumps the source database, restores it into an empty scratch database, verifies the evidence ledger there and compares it to the source. Needs the PostgreSQL client tools and a second, empty database; not run yet.

## .NET SDK and policy page

`sdk/dotnet` (`Stixis.Captain.Sdk`, net8.0, no dependencies beyond the framework): `CaptainClient` with `ChatAsync`, `StreamAsync` (async enumerable), `EmbedAsync`, `ModelsAsync`, `StatusAsync`, `UsageAsync`, typed exceptions with Captain's reason code, an idempotency key on every call, retrieved documents fenced as untrusted data; model calls are never retried, only failed connections; plain http only to this machine. `dotnet test sdk/dotnet/Stixis.Captain.Sdk.Tests`. Checked live against the gateway.

**Policy** page (`/admin/v1/policy`, `/admin/v1/policy/simulate`): shows the bundle and tier rules ACTIVE in OPA, and answers "what would the policy decide for this kind of request?" without executing, charging or recording anything. Policy changes themselves stay in git, review and signed bundles (ADR-003). Live evaluation and erasure are built (see below; decisions in `docs/adr/0005-erasure-and-live-evaluation.md`).

## Browser extension

`browser/extension` (Chrome/Edge MV3) asks `POST /v1/browser/check` before a message is submitted to a watched AI site: allow, mask or block, recorded on the BROWSER channel (never the text). Logic tests pass and the page behaviour was checked on a local test page against the live gateway (allowed, masked, blocked by kill switch). **Not verified on ChatGPT, Claude or Gemini**, and it governs only what is submitted in a browser where it is active. See `browser/extension/README.md` for what it cannot do and the managed-deployment steps.

## Function calling and approvals for chat clients

- **Streamed tool calls** now pass through `/v1/chat/completions` (the model's tool-call deltas reach the client and the stream ends with `finish_reason: "tool_calls"`; `length` is also passed). Captain does not execute the tool and does not run the text output guard over tool arguments: what a tool may do is governed where it runs (for example the MCP proxy). Checked live against a real model. A Copilot custom model can now declare `"toolCalling": true`.
- **MCP approvals for clients that cannot send `_meta`:** repeating the same call finds the approved ticket for the same caller, tool and exact arguments (single use). While a ticket is pending, repeating the call returns that ticket and never creates a second one.

## Gemini native API

`POST /v1/models/{model}:generateContent` and `...:streamGenerateContent?alt=sse` (also under `/v1beta`) accept Google's request format and answer in it, governed like every other route (same decision, masking, budget, evidence; channel API). Point the official SDK at the gateway: `genai.Client(api_key=<captain key>, http_options=types.HttpOptions(base_url="http://127.0.0.1:8000"))`. Checked with the real `google-genai` 3.6.1 SDK: generate, stream, refusal (typed `ClientError 403 PERMISSION_DENIED`) and a function call against a real model.

Limits: text, `functionCall` and `functionResponse` parts only (images, audio, files, grounding, code execution and cached content are refused with a 400, never dropped); one candidate; the key goes in `x-goog-api-key` or `Authorization: Bearer`, **not** `?key=` (URLs end up in logs); streamed function calls are sent as one complete part. Embeddings (`:embedContent`, `:batchEmbedContents`) are governed exactly like `/v1/embeddings` (text inspected and masked before the provider, charged on input tokens, recorded; text only, one shared `outputDimensionality`). `:countTokens` is approximate (4 characters per token), calls no model and costs nothing, but still needs a key. Checked with the official SDK (`embed_content` with one and many inputs, `count_tokens`, and a refusal) against the mock provider; the Google response shapes for the single `embedContent` form are from Google's REST reference, not exercised by the SDK (it always uses the batch form).

## Per-tool rate limits

`PUT /admin/v1/rate-limits` `{"scope": "tool:demo.create_issue", "requests_per_minute": 30}` limits a tool across everyone; `{"scope": "ptool:<principal>:<tool>", ...}` limits one caller on one tool (`GET` lists them; changes are recorded in the ledger). Counted only for calls that are otherwise permitted; calls held for approval are not counted (the human decision is the throttle). Over the limit the call is refused with `RATE_LIMITED` before it reaches the tool. If the counter store is down, Tier 1-2 fail closed and Tier 3-4 proceed. `DELETE /admin/v1/rate-limits?scope=...` lifts one (recorded in the ledger BEFORE it takes effect). The Budgets page has a Rate limits section to add and lift them. Checked live against PostgreSQL counters.

**Security events are capped:** the client address on a refused, unattributable request comes from a header the caller controls, so only a valid IP address is recorded (anything else is stored as `invalid`), and the ledger takes at most 60 security events a minute in total; refusals beyond that are counted and reported on the next event that gets through (`suppressed_since_last`).

**Start / stop everything (PowerShell, from `backend`):** `.\scripts\dev_up.ps1` starts OPA, the worker, the mock model, the Core, gateway, Admin API, dev IdP and the MCP proxy with its mock server (logs in `backend\.dev\logs`); `.\scripts\dev_down.ps1` stops them. PostgreSQL is yours and is left alone. No Docker.

## Dashboard queries at pilot volume

The Activity list, Coverage, Agents, **Overview** and **Usage by…** pages are computed by PostgreSQL (counts, grouping, percentiles, paging; migration 0008 indexes the ledger by time) instead of loading every event into the Admin API. On 300,000 synthetic events: coverage 0.4 s, an Activity page 0.1 s, agent stats 0.14 s, Overview about 1.1 s and Usage by… 0.6 to 1.0 s, against 9 to 10 s and 300,000 rows in memory the old way. Postgres tests (`tests/integration`) check that every SQL answer equals the in-Python reference answer, and the same check was run on the real dev database. Notes: days on the Overview chart are UTC days; the 95th percentile latency is interpolated (the same definition in SQL and in the reference). **Still the old way** (load the period into memory): Observed usage and the monthly report. They are fine at thousands of events; the monthly report is a periodic job, not an interactive page. Apply the migration with `alembic upgrade head`. If Overview gets slow at several million events, the next step is a pre-aggregated daily rollup table.

## Pilot report

`python -m captain.pilot_report --days 14 --out pilot-report.md` (or `GET /admin/v1/reports/pilot?days=14&format=md`) prints the pilot metrics from the evidence ledger: volume by channel, why calls were refused, spend by model, person and application, total and first-token latency, alerts, and the MVP acceptance checklist. Each criterion is **MET** (seen in the data), **NOT SEEN** (no example in the period yet) or **CHECK ELSEWHERE** (SSO, the egress test and secret scan, and the load test cannot be proven from Captain's own ledger and say how to run them). A sample from the dev database is in `docs/pilot-report-sample.md`. It showed, honestly, that the 80% budget alert has not been seen yet.

## Secret scan (before you commit)

`backend\.venv\Scripts\python backend\scripts\secret_scan.py` scans every file git would commit (tracked plus new, minus `.gitignore`) for (1) the exact values in your `backend\.env` (pepper, ledger key, provider keys, SSO client secret, database URL, tokens: a hit is a real leak, no false positives) and (2) known secret shapes (provider and cloud keys, private key blocks, JWTs, assigned passwords). Output never shows a full secret. Last run: 274 files against 8 real secrets and 10 shapes, nothing found. The mock-server dev constants (`CAPTAIN_MOCK_MCP_KEY`, `CAPTAIN_MOCK_PROVIDER_KEY`, the ledger key id) are public by design and skipped by name. It does not scan git history (the gitleaks job in CI does).

**Evidence export and monthly report:** they now read only the period they need (not the whole ledger), and the dashboard's ledger queries keep each entry's anchor. Before this fix an exported evidence package listed no anchors and called every event unanchored; a Postgres regression test covers it. Live on the dev ledger: 18 anchors, with the 64 newest events not yet anchored (anchors are cut periodically).

## Desktop MCP configuration check

`python -m captain.integrations.telemetry desktop-mcp --mcp-config <file> [--mcp-config <file2>] --captain-host captain.example.com` reads the MCP config of desktop AI apps (Claude Desktop / Cursor style `mcpServers`, VS Code style `servers`) and records `BYPASS_DETECTED` for each server that does not go through Captain: its name, local (stdio) or remote (http), and the remote host only. It never reads or reports arguments, environment variables, headers or tokens. A local program is always reported (a proxy cannot govern it). You pass the file paths; no app's location is assumed or verified. Feasibility study and the decisions needed for a real desktop agent: `docs/adr/0006-desktop-agent-feasibility.md`.

## Request size limits

Every HTTP service now refuses an oversized request body with HTTP 413 `CONTENT_TOO_LARGE` (Core 2 MB, Admin API 1 MB, MCP proxy 1 MB, gateway 10 MB by default; `max_body_bytes` on the gateway factory). A declared `Content-Length` over the limit is refused before the body is read; a chunked or lying upload is counted as it arrives and cut off the moment it passes the limit, so no service buffers an unbounded request. The 413 is sent immediately (FastAPI would otherwise report a body-read error as a generic 400). Note that a client still uploading may see its connection reset instead of reading the 413; the server log shows the 413. Checked live: an 11 MB and a 12 MB chunked chat request were refused, a normal chat still works, and the Admin API answered 413 for 1.2 MB.

**Rate limits apply to chat calls too, not only tools.** `PUT /admin/v1/rate-limits` takes `requests_per_minute` and/or `tokens_per_minute` on any budget scope (`principal:`, `key:`, `app:`, `project:`, `team:`, `cost_centre:`, `org:`, `model:`) as well as the tool scopes. A model call that would pass the limit is refused with HTTP 429 `RATE_LIMITED` before the provider is called; it costs nothing and holds nothing back. The token limit counts the input plus the most the call could produce (`max_tokens`), so it also stops one huge prompt. Checked live: a limit of 3 requests a minute on `principal:dev-app` let 3 calls through and refused the next 2, then lifting it restored service; the refusals are in the evidence ledger. The Budgets page form sets both limits.

## Testing guide

`docs/testing-guide.md` is the step-by-step manual test plan for VS Code and the dashboard (T0 baseline, C1 to C8, optional extras, a result sheet and troubleshooting): what to type, what to click, and exactly what you should see. `backend\scripts\dev_reset.py` (`--check`, default reset, `--arm-c5`) puts the dev environment in the known state the guide assumes.

## Live evaluation (Tier 1-2)

Set `CAPTAIN_JUDGE_URL` (an OpenAI-compatible endpoint inside your perimeter) and `CAPTAIN_JUDGE_MODEL` on the gateway, and a sample of Tier 1-2 model calls (5% by default, `CAPTAIN_EVAL_SAMPLE_RATE`, per system `CAPTAIN_EVAL_RATES='{"<register_id>": 0.2}'`) is scored by that judge for groundedness, policy compliance and safety against `policies/evaluation/rubric.json`. The judge sees only the masked prompt and masked answer, in memory, after the caller has been answered; nothing is stored and there is no hosted fallback. The ledger gets `evaluation.scored` (scores, rubric version, judge model) or `evaluation.failed`; an overall score below 0.6 raises an `evaluation.low_score` alert. The Agents page shows the average score per system. Unset `CAPTAIN_JUDGE_URL` = evaluation off. Not yet checked against a real judge model.

## Erasure (crypto-shredding)

People's ids and client IPs never reach the signed evidence ledger in the clear: the ledger holds a pseudonym and the person's key lives in `subject_keys` (migration 0009, run `alembic upgrade head`). The dashboard and reports show people as usual. `POST /admin/v1/subjects/<id>/erase` deletes the key after the person's keys are revoked: from then on their events show as "erased subject", totals and ledger verification are unchanged, and it cannot be undone. Service accounts, applications and agents stay readable. Client IPs are hashed with a daily key that the worker deletes after 30 days. Erasing also renames the person to the same pseudonym in every editable record (principal, keys, approvals, alerts, budgets, rate limits, kill switches) in one transaction. There is an **Erase a person** button on the Keys page. Events written before this change still hold ids in the clear. Procedure, backups and what it does not cover: `docs/runbooks.md` section 9.

## Desktop agent (Windows, v1)

`desktop/windows/Invoke-CaptainDesktopAgent.ps1` reports, once a day, which AI desktop apps (ChatGPT, Claude Desktop, Cursor, VS Code) are installed or running and which MCP servers they are configured to use outside Captain. Metadata only: never content, paths, arguments or tokens; `-DryRun` prints exactly what it would send. `Register-CaptainDesktopAgent.ps1` installs it as a scheduled task (Intune-friendly). It posts to `POST /v1/desktop/report` with a fleet key whose principal has the `desktop-agent` role (dev: `python scripts/dev_desktop.py`). Evidence: OBSERVED inventory and BYPASS_DETECTED signals on the DESKTOP channel. Decisions, what is collected, and limits: `docs/adr/0006-desktop-agent-feasibility.md`. Employee notice must be confirmed by each client before deployment.

## Azure deployment (E2)

`deploy/opentofu/azure` creates one client's installation in their own subscription: private network, AKS with workload identity, PostgreSQL (backups capped at 30 days for erasure), Key Vault holding the ledger signing key (`CAPTAIN_LEDGER_KEY=azurekv:<key URL>`, signing never leaves Key Vault), and a write-once anchor container (Captain now refuses to start if the container has no immutability policy). The Helm chart gained the MCP proxy, OPA running only the signed bundle, a service account for workload identity, and Key Vault secret sync. Build the image with `--build-arg EXTRAS=azure`. Step-by-step: `deploy/opentofu/azure/README.md`. Checked with OpenTofu 1.13.10 (`tofu validate`, `tofu fmt -check`) and Helm 4.3.2 (`helm lint`, `helm template`, default and Azure values); both tools are in `tools/` (gitignored). **Not yet applied to a real subscription.**

