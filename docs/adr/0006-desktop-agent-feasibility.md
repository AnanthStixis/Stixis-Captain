# ADR-006: Desktop agent: feasibility and a recommended first version

- Status: Accepted 2026-11-26 for a first version (Windows, observe-only). Built: see "v1 as built" at the end.
- Date: 2026-11-25
- Code so far: `backend/captain/integrations/telemetry/desktop_mcp.py` (the one signal that needs no agent)

## The question
Employees use AI in desktop apps (ChatGPT, Claude Desktop, Codex and similar CLIs, IDE assistants). Captain cannot sit in the middle of those apps the way it sits behind an API key. What can a desktop agent honestly do, and what should it never claim?

## What is feasible, signal by signal

| Signal | Feasible? | What Captain can then say |
|---|---|---|
| **Which AI apps are installed or running** (process / install inventory) | Yes, standard OS facilities, no special rights | "This device has app X" (inventory, not use) |
| **MCP configuration of those apps** (which servers each app is set to use) | Yes, read the app's JSON config | "App X is configured to use server Y directly, not through Captain": BYPASS signal. **Built** (see below) |
| **Endpoint settings of CLI tools** (for example an API base-URL environment variable) | Yes to check; setting it is done by device management | "This tool points at Captain" or "does not". Governed only if it does point at Captain |
| **Network destinations** (which AI hosts the device contacts, how much) | Partly: needs an OS network component or the company's proxy/DNS logs. Logs are the lower-risk route and are already supported by `discovery` | "This device reached AI host H N times": BYPASS signal, no content |
| **What was typed or sent** (prompt content) | Not without intercepting encrypted traffic or reading the screen | Nothing. See "rejected" |
| **Blocking** an app or a request | Only through the company's existing device or network controls, not through an observe-first agent | Out of scope for version 1 |

## Rejected for version 1
- **TLS interception** (a man-in-the-middle certificate on the device): it breaks pinned apps, creates a high-value target, and turns Captain into something that reads everything employees type. Not recommended.
- **Screen or accessibility scraping** to read prompts: highly invasive and fragile, with real works-council and privacy exposure.
- **Calling anything "governed"** that Captain did not decide before it happened. A desktop agent produces `OBSERVED` or `BYPASS_DETECTED` evidence only. The coverage model stays honest: GOVERNED requires a path through Captain.

## Recommended first version
A small, read-only, per-user check that runs on a schedule through the company's device management and reports metadata only:
1. **MCP configuration check** per app (built, below).
2. **Installed AI apps** inventory.
3. **Endpoint setting check** for the CLI tools in scope.
4. Optionally, **network metadata** from existing proxy or DNS logs (already supported), not from a new device driver.

Fix-ups (rewriting an app's MCP config to point at Captain's proxy, setting the base-URL variable) are done by the device management tool using a Captain-provided config, so the change is the company's, visible and reversible, not a hidden action by an agent.

## What is built now
`python -m captain.integrations.telemetry desktop-mcp --mcp-config <file> --captain-host <host>`:
- reads Claude Desktop / Cursor style (`mcpServers`) and VS Code style (`servers`) JSON;
- reports, per server NOT going through Captain, only its name, whether it is local (stdio) or remote (http), and the remote host name, as `BYPASS_DETECTED`;
- never reads or reports arguments, environment variables, headers or tokens (tests prove a secret in each place never reaches the record);
- a local (stdio) server is always reported, because an HTTP proxy cannot govern a local program.
It is not tied to any app's file location: the caller passes the paths. Run on a fixture and on this project's own `mcp.json` (which correctly shows one server going through Captain and zero bypasses).

## Privacy and legal (must be settled before any rollout)
Device-level monitoring is regulated in many places. Before pilot: employee notice and, where required, consultation; a stated purpose and retention; metadata only (no content, no file paths, no arguments); machine-user names pseudonymised (already true of every importer).

## DECIDE
1. **Which operating systems first** (the plan says Windows is likely; macOS needs a different device-management route).
2. **Which device-management tool** the clients use (it carries the schedule and any fix-up config).
3. **Which desktop apps are in scope** for the first client (their config file locations and versions must then be verified on real installs; none is verified here).
4. **Confirm TLS interception and screen reading stay out of scope.**
5. **Who owns employee notice** for device-level metadata.

## Decisions (2026-11-26)
| Question | Decision |
|---|---|
| Operating system first | **Windows** |
| Device management | **Intune** (or any tool that can run a user-context PowerShell script) |
| Apps in scope | **ChatGPT, Claude Desktop, Cursor, VS Code** |
| TLS interception, screen reading | **Out of scope**, confirmed |
| Who owns employee notice | **Still open.** It belongs to each client (their works council / privacy office); Captain supplies the exact list of what is collected (below). Do not deploy before the client confirms notice is given. |

## v1 as built
- **Agent:** `desktop/windows/Invoke-CaptainDesktopAgent.ps1` (Windows PowerShell 5.1, no install needed) and `Register-CaptainDesktopAgent.ps1` (scheduled task at logon and daily, user context; `-Uninstall`).
- **What leaves the machine, once a day:** the agent version; a SHA-256 of the Windows machine id (never the machine name); the user's UPN or `DOMAIN\user` (mapped to a Captain principal through `CAPTAIN_DESKTOP_ACTOR_MAP`, otherwise hashed; the ledger then stores a pseudonym, ADR-005); the UTC day; per app installed / running / version; per MCP server its name, `stdio` or `http`, and the remote host. Never content, file paths, command arguments, environment variables, headers or tokens. `-DryRun` prints exactly this report.
- **Receiver:** `POST /v1/desktop/report` on the gateway. The report format is closed (an unknown field, a machine name, a path or arguments are refused with 422 and nothing is recorded). Only a key whose principal has the `desktop-agent` role may report; the fleet's system allows no models, so the key cannot be used to chat. Apps become OBSERVED inventory events, non-Captain MCP servers BYPASS_DETECTED events, both on the DESKTOP channel; re-sending a day changes nothing. MCP servers whose host is in `CAPTAIN_PUBLIC_HOSTS` (or the gateway's own host) are counted, not reported.
- **Checked:** unit tests for the endpoint (format, role, idempotency, hashing of unmapped users); a dry run on a real Windows 11 machine (ChatGPT, Claude and VS Code detected); a run against a fake config full of secrets showing none of them in the report; a live report recorded on the dev stack.

## Known limits of v1
- App install locations are the vendors' defaults; an app installed elsewhere is still found while it is running (its process proves it). A "ChatGPT" process may be a web-app wrapper; its version is then the browser engine's.
- MCP servers are read from the apps' JSON config files. Servers that VS Code (Copilot) keeps in its own internal storage after its 2026 MCP migration are not visible to v1.
- Observe-only by design: nothing is blocked, and an app that is installed but never run shows only as installed.

