# Shared Captain on the QA Docker server

**One** Captain serves Stixis and every company (ADR-008). Adding a company is a click on
the Stixis console; it is live at once at its own address, with its own data:

| Address | Who |
|---|---|
| **captain.stixis.com** | Stixis super admin: Dashboard, Companies, Stixis staff, Audit |
| **abc.stixis.com** | company ABC: its org admin, users, AI keys, models, budgets, keys, SSO |
| **xyz.stixis.com** | company XYZ, and so on: nothing to deploy or configure per company |

Inside one address the path picks the service: `/v1/…` gateway (VS Code, SDKs, API),
`/core/v1/…` Core (Claude Code, desktop agent), `/mcp` MCP proxy, everything else the console.

What is shared and what is not:
- **Shared:** the programs (console, gateway, Core, MCP, worker), one PostgreSQL server.
- **Per company:** its own database schema (`t_<code>`), evidence ledger and signing key,
  anchors, users, AI keys, models and prices, budgets, Captain keys, SSO, activity.
  A company's address only ever reaches its own schema.
- **Fair share:** each company has its own AI-call limit per minute (`CAPTAIN_TENANT_RPM`),
  its own budgets and its own provider keys, so a busy company does not slow the others.

Only ports **80 and 443** are published (free on this server; the existing containers use
8000-8013, 5000-5001, 7365, 7373, 8443, 9000).

## 0. Before you start
- **DNS:** `*.stixis.com` (wildcard) points to the QA server's IP. For a quick test, add
  `captain.stixis.com` and each company's name to the hosts file of the testing machines.
- **Certificate:** a `*.stixis.com` wildcard certificate (recommended). Without one, use
  `http` below (closed network only).
- `sudo ss -ltnp | grep -E ':(80|443) '` prints nothing.

## 1. Build the image (on the QA server, from the repository root)
```sh
git clone https://github.com/AnanthStixis/Stixis-Captain.git captain && cd captain
docker build -f deploy/qa/Dockerfile -t stixis/captain:qa .
```

## 2. Settings and certificate (once)
```sh
cd deploy/qa
sh make-env.sh stixis.com certs        # or: sh make-env.sh stixis.com http
cp /path/to/wildcard.crt edge/certs/wildcard.crt
cp /path/to/wildcard.key edge/certs/wildcard.key
```
`qa.env` (secrets) and `edge/Caddyfile` are generated and never committed.

## 3. Start
```sh
docker compose --env-file qa.env up -d
docker compose --env-file qa.env ps        # all running; "init" exited (0)
```
**Expected:** `https://captain.stixis.com/admin/v1/health` answers `{"status":"ok"}`.

## 4. First Stixis super admin (no SSO yet)
```sh
docker compose --env-file qa.env exec admin \
  python -m captain.first_admin you@stixis.com --name "Your Name" --platform-admin
```
Open the printed `https://captain.stixis.com/accept-invite#…` link. It works once, for 7 days.

## 5. Add a company (the only per-company step)
1. **Companies → Add a company** (code `abc`, name, the org admin's email...).
   **Expected:** "Company storage: Created. The company's Captain is at https://abc.stixis.com".
2. **Bootstrap** → copy the one-time link and send it to the org admin.
3. **Check now** → 1 user (1 org admin).

## 6. The org admin (https://abc.stixis.com)
Signs in with the link; **Setup** walks through: AI key → AI use → model and price →
spending limit → Captain key (every step opens right there), and SSO if wanted.

VS Code: Settings → `captain.gatewayUrl` = `https://abc.stixis.com`; Copilot custom model URL
`https://abc.stixis.com/v1/chat/completions` with the Captain key and the model name.

## Day-to-day (in `deploy/qa`)
| Task | Command |
|---|---|
| Logs | `docker compose --env-file qa.env logs -f gateway` |
| More capacity | `docker compose --env-file qa.env up -d --scale gateway=2 --scale core=2` |
| Update to a new version | `git pull`, rebuild the image (step 1), `docker compose --env-file qa.env up -d` (`init` migrates Stixis and every company) |
| Back up everything | `docker compose --env-file qa.env exec postgres pg_dump -U captain captain > captain.sql` |
| Back up one company | `... pg_dump -U captain -n t_abc captain > abc.sql` |
| Stop | `docker compose --env-file qa.env down` (data stays) |

## QA differences from production
- OPA reads `policies/` directly; production runs only a signed policy bundle (Helm chart).
- Ledger signing keys and anchors live in the `data` volume (one folder per company);
  production uses Key Vault and immutable Blob storage.
- Keep exactly one `worker` container: it runs one ledger writer per company.
- A company that requires its own cloud or region can still get a dedicated install
  (the separate-install path, ADR-007).
