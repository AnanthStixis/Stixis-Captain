# Desktop agent (Windows)

**What Captain gets:** once a day, which AI desktop apps (ChatGPT, Claude Desktop, Cursor, VS Code) are installed or running, and which MCP tools those apps use outside Captain. **Observe only:** it blocks nothing and never reads content, file paths, command arguments or tokens.

**Before deploying to employees:** confirm employee notice (and consultation where required) for device-level metadata.

## Before you start
- One **fleet key** for all computers: a Captain key whose principal has the `desktop-agent` role and whose AI system allows no models (ask Stixis or your platform team).

## Install
1. Download **Desktop agent for Windows** (`captain-desktop-agent.zip`) and unzip it. `captain.json` already holds your address (`{{GATEWAY_URL}}`).
2. Try it without sending anything:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\Invoke-CaptainDesktopAgent.ps1 -CaptainUrl {{GATEWAY_URL}} -DryRun
   ```
   **Expected:** it prints exactly what it would send.
3. Install for a user (runs at logon and daily at 12:00):
   ```powershell
   .\Register-CaptainDesktopAgent.ps1 -KeyFile \\share\captain\desktop-fleet.key
   ```
   **Intune:** deploy `Register-CaptainDesktopAgent.ps1` as a user-context script (or a Win32 app with `-Uninstall` as the uninstall command).

## Test it
Run the agent once (step 2 without `-DryRun`, with `-KeyFile`). **Expected:** "recorded N: ... app(s)". **Activity** shows "VS Code was running on a computer used by ...", and MCP tools outside Captain show as "Possible bypass".
