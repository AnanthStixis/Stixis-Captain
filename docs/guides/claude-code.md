# Claude Code

**What Captain governs:** every tool Claude Code is about to run (reading and editing files, shell commands). Captain allows it, refuses it, or holds risky ones for a person's approval. Each decision is recorded.

**Honest limit:** this governs tools, not the model's text. A developer can remove a user-level hook; for enforcement, install it in Claude Code's **managed settings**.

## Before you start
- A **Captain key** for each person or system. An administrator creates it on the **Captain keys** page (choose the person, the AI system and the project). People never get the company's AI provider keys.
- Python 3.10+ and Claude Code installed.
- Captain Core address: `{{CORE_URL}}`

## Install
1. Download **Captain hook for Claude Code** (`captain-claude-code.zip`) and unzip it, for example to `~/.captain/`.
2. Save your Captain key in `~/.captain/key` (one line, only you can read it).
3. Merge the `env` block from `captain-settings-snippet.json` into `~/.claude/settings.json` (or your organisation's managed settings).
4. Run `python ~/.captain/captain_hook.py --print-settings` and add the printed `hooks` block to the same settings file.

## Test it
1. In Claude Code ask it to read a file. **Expected:** it runs. **Activity** shows "<you> ran the tool claudecode.Read."
2. Ask it to run a shell command. **Expected (with the default setup):** it is held, waiting for approval; **Approvals** shows the request. Approve it and ask Claude Code to try again: it runs once.

## If something goes wrong
| You see | Likely cause | Fix |
|---|---|---|
| Every tool refused with "not registered" | The tool is not in Captain's Models & tools | Ask your admin to register `claudecode.<Tool>` |
| Hook errors about the key | Missing `~/.captain/key` | Save the key there |
