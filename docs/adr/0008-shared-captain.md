# ADR-008: One shared Captain for many companies

- Status: Accepted 2026-11-26. Supersedes the "one install per company" decision of
  ADR-007 for normal customers; dedicated installs remain for customers who require them.
- Related: ADR-007 (product model), `captain/tenancy.py`, `deploy/qa/`

## Context
Registering a company under ADR-007 meant deploying and configuring a whole Captain stack
(containers, database, keys, address) per company. Stixis wants adding a company to be a
click, and one deployment to upgrade.

## Decision
| Question | Decision |
|---|---|
| How companies share Captain | **One deployment** (console, gateway, Core, MCP, worker) serves every company. |
| Data separation | **One PostgreSQL schema per company** (`t_<code>`); Stixis's control plane in the base schema. Each company's copy of the app is built with a connection bound to its schema, so its queries cannot reach another company's tables. |
| How a request finds its company | **By web address**: `<code>.<base domain>` (ABC at `abc.stixis.com`), Stixis at `captain.<base domain>`. Unknown or suspended addresses get 404. One wildcard DNS entry and certificate. |
| What is per company | Ledger and signing key, anchors, users, AI keys, models and prices, budgets, Captain keys, SSO, cross-instance messages (own LISTEN/NOTIFY channel), ledger-writer lock. |
| Adding a company | Stixis console **Add a company** creates the schema, migrates it, creates its signing key and sets its address; Bootstrap and health checks run in-process. |
| Upgrades | `python -m captain.tenancy migrate-all` (the `init` step) migrates every schema. |
| Fairness and load | Stateless services scale by copies; per-company AI-call limit (`CAPTAIN_TENANT_RPM`), budgets and provider keys; small per-company connection pools (`CAPTAIN_TENANT_DB_POOL_SIZE`). |
| Code inside a company | Unchanged: the single-company code runs per company with that company's settings (context-local overlay, `captain.envfile.tenant_env`). |

## Consequences
- A company is live seconds after Stixis adds it; nothing to deploy.
- All companies share one database server: back up per company with `pg_dump -n t_<code>`.
- Memory grows with the number of active companies (one app copy per company per
  service); fine for tens of companies, revisit (shared app, schema switch per request)
  beyond that.
- A very large or regulated company can still get a dedicated install (ADR-007 path).

## Verified
Live on PostgreSQL: two companies added through the Stixis console, each bootstrapped and
health-checked in-process; a user added in one is invisible to the other and to Stixis;
each company has its own schema; one worker process writes every company's ledger.
Unit tests: address routing, unknown addresses, per-company settings, rate limit, per-company
message channel and ledger lock (`tests/unit/test_tenancy.py`).
