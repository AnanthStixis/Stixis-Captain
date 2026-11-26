# Captain desktop agent for Windows (v1)

Observe-only: it reports which AI desktop apps are installed or running and which MCP servers they use outside Captain. It never blocks anything and never sends content. Full description, decisions and limits: `docs/adr/0006-desktop-agent-feasibility.md`.

**Before deploying to employees:** the client must confirm employee notice (and consultation where required) for device-level metadata.

## Try it (no sending)
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Invoke-CaptainDesktopAgent.ps1 -CaptainUrl https://captain.corp.example -DryRun
```
It prints exactly what would be sent.

## Install for a user
```powershell
.\Register-CaptainDesktopAgent.ps1 -CaptainUrl https://captain.corp.example -KeyFile \share\captain\desktop-fleet.key
```
Runs at logon and daily at 12:00. Remove with `.\Register-CaptainDesktopAgent.ps1 -Uninstall`.

**Intune:** deploy `Register-CaptainDesktopAgent.ps1` as a PowerShell script in **user** context (or as a Win32 app with `-Uninstall` as the uninstall command), with the fleet key placed where the script can read it.

## The fleet key
One key for the whole fleet. Its principal has the `desktop-agent` role and its AI Register entry allows **no models and no tools**, so the key can only send desktop reports. Revoking it stops all agents at once.

Server settings (gateway): `CAPTAIN_PUBLIC_HOSTS` (comma list of Captain host names: MCP servers on these are correctly governed and not reported) and optionally `CAPTAIN_DESKTOP_ACTOR_MAP` (a JSON file mapping `user@corp` or `DOMAIN\user` to Captain principal ids; unmapped users are hashed).
