# MCP tools for AI agents

**What Captain governs:** tool calls from AI agents (VS Code agent mode, Claude Desktop, Cursor, your own agents). Only tools registered in Captain and allowed for the agent's AI system can run; risky actions wait for a person's approval, which works once for that exact action.

## Before you start
- A **Captain key** for the agent (Captain keys page), tied to the agent's AI system.
- The tools registered on **Models & tools** (MCP server + tools), and allowed on the agent's **AI Register** entry.
- Captain MCP address: `{{MCP_URL}}`

## Install
The key is read from the environment variable `CAPTAIN_AGENT_KEY`. Set it once, for your user, then restart the app:
```powershell
[Environment]::SetEnvironmentVariable("CAPTAIN_AGENT_KEY", "<the agent's Captain key>", "User")
```
- **VS Code:** download **MCP settings for VS Code** (`mcp.json`) into your project's `.vscode` folder (or your user MCP settings). In Copilot Chat use **Agent** mode.
- **Claude Desktop:** merge **MCP for Claude Desktop** into `%APPDATA%\Claude\claude_desktop_config.json` (needs Node.js for `mcp-remote`).
- **Cursor:** merge **MCP settings for Cursor** into `%USERPROFILE%\.cursor\mcp.json`.

## Test it
1. Ask the agent to use a read-only tool. **Expected:** it runs; **Activity** shows "<agent> ran the tool ...".
2. Ask for a write action that needs approval. **Expected:** the agent says it needs approval and gives a ticket id; **Approvals** shows the same ticket.
3. An approver approves it; ask the agent to call the tool again with the same input. **Expected:** it runs once. Asking again needs a new approval.

## If something goes wrong
| You see | Likely cause | Fix |
|---|---|---|
| The app asks for an "OAuth client ID" | The key was not sent | Set `CAPTAIN_AGENT_KEY` and fully restart the app |
| Tools missing in VS Code | Tool names, or tool calling off | Reload; check the model has tool calling on |
| "Tool not allowed" | Not allowed for this AI system | Admin: AI Register → allowed tools |
