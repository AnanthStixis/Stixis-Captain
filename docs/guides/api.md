# Direct API (any tool whose AI address you can change)

**What Captain governs:** any tool or code that speaks the OpenAI, Anthropic or Gemini API: point it at Captain instead of the provider, with a Captain key instead of the provider key.

| Format | Address to use |
|---|---|
| OpenAI chat completions / responses / embeddings | `{{GATEWAY_URL}}/v1` |
| Anthropic messages | `{{GATEWAY_URL}}` (Anthropic SDK `base_url`) |
| Gemini | `{{GATEWAY_URL}}` (google-genai `http_options.base_url`) |

## Test it
```powershell
curl.exe {{GATEWAY_URL}}/v1/chat/completions -H "Authorization: Bearer <your Captain key>" -H "Content-Type: application/json" -d '{"model":"claude-sonnet","messages":[{"role":"user","content":"Say hello"}],"max_tokens":20}'
```
**Expected:** a normal answer, and the response headers include `X-Captain-Decision: ALLOW`. **Activity** shows the request.
