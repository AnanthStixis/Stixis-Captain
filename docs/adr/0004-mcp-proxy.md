# ADR 0004 — MCP Proxy

Status: accepted (Phase 3)

## Decisions
- **One stateless endpoint for both protocol eras.** `POST /mcp` answers 2026-07-28 requests (`server/discover`, per-request `_meta`) and 2025 handshake clients (`initialize`). No `Mcp-Session-Id` is issued; a stateless server is allowed and survives restarts and replicas. Verified with the official MCP Python SDK in `auto` and `legacy` modes (the SDK's strict result validation caught a wrong guess about the modern result envelope, which is why wire shapes are checked against it rather than assumed).
- **Core decides, the proxy executes.** The proxy builds a `GovernanceRequest` (channel MCP, action TOOL_CALL) and calls `decide`. New Core operations are behind `McpCoreAPI` (`list_tools`, `tool_target`, `screen_tool_output`); integrations still import only `captain.core.decision_api` and `captain.core.models` (enforced by the architecture test).
- **Approval by retry.** A held call returns an error tool result with a ticket; the agent repeats the call with `_meta["com.stixis.captain/approval_ticket"]`. The ticket is bound to principal, register entry, **tool** and exact argument hash, single use, and the Core re-evaluates kill switch and policy when it is consumed. (Binding to the tool was missing and added in this phase.)
- **Tool results are untrusted.** Tier 1–2 refuse secrets and likely prompt injection and mask personal data; Tier 3–4 pass text through and record findings. Non-text content is dropped with a note. Evidence (`tool.executed`) holds outcome, latency and finding names, never arguments or results; at Tier 1–2 the output is withheld if that evidence cannot be written.
- **Upstream safety.** https only (http to loopback in development), link-local/metadata/unspecified/multicast addresses always refused, no redirects, 5 s connect and 30 s total timeouts, 1 MB response cap, credential resolved at call time from `credential_ref`.
- **Browsers cannot call it**: any `Origin` outside an explicit allow-list gets 403.

## Not done
Per-tool money budgets (tools have no cost), server-push streams, sampling/elicitation, batch requests, OAuth resource-metadata discovery for MCP clients that require it, verification against third-party servers.
