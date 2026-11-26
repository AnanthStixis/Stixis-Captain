# VS Code with GitHub Copilot

**What Captain governs:** Copilot chat and agent requests made with the **Captain** model (personal data hidden, secrets blocked, budgets applied, every request recorded) and, in agent mode, MCP tool calls (see the MCP guide).

**Honest limit:** Copilot's own built-in models do not go through Captain. Ask people to use the Captain model, or turn the other models off in your GitHub organisation's Copilot policy.

## Before you start
- A **Captain key** for each person or system. An administrator creates it on the **Captain keys** page (choose the person, the AI system and the project). People never get the company's AI provider keys.
- VS Code with GitHub Copilot signed in.
- Captain address: `{{GATEWAY_URL}}`

## Install
1. From **Connect your tools → VS Code**, download **Captain extension for VS Code** (`captain-vscode.vsix`). In VS Code: **Extensions → ⋯ → Install from VSIX…** and pick the file.
2. `Ctrl+Shift+P` → **Captain: Set Key** → paste your Captain key.
   **Expected:** the status bar shows your AI system and tier, for example "Captain: AI-DEV · Tier 3".
3. Add the Captain model to Copilot: download **Copilot model settings** (`chatLanguageModels.json`). In VS Code: `Ctrl+Shift+P` → **Chat: Manage Language Models** → add a custom endpoint with:
   - URL `{{GATEWAY_URL}}/v1/chat/completions`, API type *chat completions*
   - API key: your Captain key
   - model id `claude-sonnet` (or the model your admin enabled), **tool calling on**
   (Or copy the downloaded file's entry into `%APPDATA%\Code\User\chatLanguageModels.json` and replace `<your Captain key>`.)
4. In Copilot Chat, pick the **Captain** model.

## Test it
1. Ask: `Say hello`. **Expected:** a normal answer. On the dashboard, **Activity** shows "<you>'s request to claude-sonnet was allowed."
2. Ask: `My email is test.person@example.com, repeat it back`. **Expected:** the answer does not contain the address. **Activity** shows "Allowed, data hidden" and "An email address was hidden first."
3. **Connect your tools → VS Code** shows calls in the last 7 days.

## If something goes wrong
| You see | Likely cause | Fix |
|---|---|---|
| Status bar "key rejected" | Wrong or revoked key | **Captain: Set Key** again; ask your admin for a new key |
| Status bar "gateway unreachable" | Wrong address or network | Check `{{GATEWAY_URL}}/health` opens in a browser |
| Copilot says it cannot use tools | Tool calling off on the custom model | Set `"toolCalling": true` and reload VS Code |
| Error mentions budget | Budget used up | Ask your admin (Budgets page) |
