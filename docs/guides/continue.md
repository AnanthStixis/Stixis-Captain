# Continue (VS Code or JetBrains)

**What Captain governs:** chat and autocomplete requests that Continue sends to the Captain model.

## Before you start
- A **Captain key** for each person or system. An administrator creates it on the **Captain keys** page (choose the person, the AI system and the project). People never get the company's AI provider keys.
- Continue installed. In VS Code also install the Captain extension (see the VS Code guide).

## Install
- **VS Code:** `Ctrl+Shift+P` → **Captain: Open Continue Configuration**. It shows a ready configuration for `{{GATEWAY_URL}}/v1`; copy the model entry into your Continue config. It never overwrites your own settings.
- **JetBrains / by hand:** add a model to Continue's config with provider `openai`, `apiBase` `{{GATEWAY_URL}}/v1`, `apiKey` your Captain key, model `claude-sonnet` (or the one your admin enabled).

## Test it
1. Ask Continue: `Say hello`. **Expected:** a normal answer; **Activity** shows your request as allowed.
2. **Connect your tools** shows calls in the last 7 days.
