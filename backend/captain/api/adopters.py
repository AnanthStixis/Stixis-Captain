"""Adopters: the ways a company's people and systems use AI through Captain (plan step 4).

The org admin picks the adopters the company will use. For each one the dashboard shows
what it governs, its downloads (preset with THIS install's addresses, never a key), a
step-by-step install guide and a "test it" checklist (docs/guides/<guide>.md).

Downloads are built from release artifacts in `CAPTAIN_ARTIFACTS_DIR`:
    ide/vscode/captain-vscode-*.vsix   browser/extension/dist/   desktop/windows/
    ide/claude-code/captain_hook.py
(the repository layout; `scripts/build_artifacts.ps1` produces the same layout for images).
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Urls:
    """Where this install is reached (Helm/env: CAPTAIN_PUBLIC_*_URL)."""

    gateway: str = "http://127.0.0.1:8000"
    mcp: str = "http://127.0.0.1:8300/mcp"
    core: str = "http://127.0.0.1:8080"
    console: str = "http://127.0.0.1:8100"

    def fill(self, text: str) -> str:
        return (
            text.replace("{{GATEWAY_URL}}", self.gateway)
            .replace("{{MCP_URL}}", self.mcp)
            .replace("{{CORE_URL}}", self.core)
            .replace("{{CONSOLE_URL}}", self.console)
        )


@dataclass(frozen=True)
class Download:
    id: str
    label: str
    filename: str
    note: str


@dataclass(frozen=True)
class Adopter:
    id: str
    name: str
    group: str
    governs: str
    channel: str  # evidence channel its traffic is recorded on (for "seen recently")
    guide: str
    downloads: tuple[Download, ...] = field(default_factory=tuple)


CATALOG: tuple[Adopter, ...] = (
    Adopter(
        "vscode", "VS Code (GitHub Copilot)", "Developer tools",
        "Copilot chat and agent requests made with the Captain model, and MCP tools in agent mode.",
        "API", "vscode",
        (Download("vscode-extension", "Captain extension for VS Code", "captain-vscode.vsix",
                  "Shows the connection status and sets up the key. Preset with your address."),
         Download("vscode-copilot-model", "Copilot model settings", "chatLanguageModels.json",
                  "Adds the Captain model to Copilot. Each person adds their own key."),
         Download("mcp-vscode", "MCP settings for VS Code", "mcp.json",
                  "Routes agent tool calls through Captain.")),
    ),
    Adopter(
        "claude-code", "Claude Code", "Developer tools",
        "Every tool Claude Code runs (file edits, shell commands) is checked by Captain first.",
        "HOOK", "claude-code",
        (Download("claude-code-hook", "Captain hook for Claude Code", "captain-claude-code.zip",
                  "The hook script and a settings snippet, preset with your address."),),
    ),
    Adopter(
        "continue", "Continue (VS Code / JetBrains)", "Developer tools",
        "Chat and autocomplete requests from Continue.", "API", "continue",
    ),
    Adopter(
        "browser", "Browser extension (ChatGPT, Claude, Gemini websites)", "Everyday AI use",
        "Text typed into AI websites is checked before it is sent: personal data is hidden, "
        "secrets are blocked.", "BROWSER", "browser",
        (Download("browser-extension", "Browser extension (Chrome / Edge)", "captain-browser.zip",
                  "Preset with your address. Load unpacked to test, or deploy with Intune / GPO."),
         Download("browser-policy", "Intune / GPO policy", "captain-browser-policy.json",
                  "Sets the address for every employee. Add each user's key with your tool.")),
    ),
    Adopter(
        "mcp", "MCP tools for AI agents", "Agents",
        "Tool calls from AI agents: allowed tools only, risky actions wait for a person's "
        "approval.", "MCP", "mcp",
        (Download("mcp-vscode", "MCP settings for VS Code", "mcp.json", ""),
         Download("mcp-claude-desktop", "MCP for Claude Desktop", "claude_desktop_config.json", ""),
         Download("mcp-cursor", "MCP settings for Cursor", "cursor-mcp.json", "")),
    ),
    Adopter(
        "sdk-python", "Python SDK", "Applications",
        "Model calls from your own Python applications.", "API", "sdk-python",
    ),
    Adopter(
        "sdk-js", "JavaScript / TypeScript SDK", "Applications",
        "Model calls from your own Node.js applications.", "API", "sdk-js",
    ),
    Adopter(
        "sdk-dotnet", ".NET SDK", "Applications",
        "Model calls from your own .NET applications.", "API", "sdk-dotnet",
    ),
    Adopter(
        "api", "Direct API (OpenAI, Anthropic, Gemini compatible)", "Applications",
        "Any tool that lets you change its AI address: point it at Captain instead of the "
        "provider.", "API", "api",
    ),
    Adopter(
        "desktop-agent", "Desktop agent (Windows)", "Visibility",
        "Shows which AI desktop apps are installed or running, and MCP tools set up outside "
        "Captain. Observe only: it blocks nothing.", "DESKTOP", "desktop-agent",
        (Download("desktop-agent", "Desktop agent for Windows", "captain-desktop-agent.zip",
                  "Preset with your address. Deploy with Intune as a user-context script."),),
    ),
)  # fmt: skip

BY_ID = {a.id: a for a in CATALOG}
DOWNLOAD_IDS = {d.id for a in CATALOG for d in a.downloads}


class ArtifactMissingError(Exception):
    """The release artifact for a download is not present on this server."""


# ----------------------------------------------------------------- builders
def vscode_vsix(artifacts: Path, urls: Urls) -> bytes:
    """The released .vsix with `captain.gatewayUrl` defaulting to this install."""
    found = sorted((artifacts / "ide" / "vscode").glob("captain-vscode-*.vsix"))
    if not found:
        raise ArtifactMissingError("the VS Code extension package (.vsix) has not been built")
    src = zipfile.ZipFile(found[-1])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "extension/package.json":
                pkg = json.loads(data)
                props = pkg["contributes"]["configuration"]["properties"]
                props["captain.gatewayUrl"]["default"] = urls.gateway
                data = json.dumps(pkg, indent=2).encode()
            z.writestr(item, data)
    return out.getvalue()


def browser_zip(artifacts: Path, urls: Urls) -> bytes:
    """The built extension, allowed to talk to THIS install only, with the address preset."""
    dist = artifacts / "browser" / "extension" / "dist"
    if not (dist / "manifest.json").exists():
        raise ArtifactMissingError("the browser extension has not been built")
    out = io.BytesIO()
    origin = f"{urlsplit(urls.gateway).scheme}://{urlsplit(urls.gateway).netloc}"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(dist.rglob("*")):
            if not f.is_file() or f.name == "preset.json":
                continue
            data = f.read_bytes()
            if f.name == "manifest.json":
                m = json.loads(data)
                m["host_permissions"] = [f"{origin}/*"]
                data = json.dumps(m, indent=2).encode()
            z.writestr(f.relative_to(dist).as_posix(), data)
        z.writestr("preset.json", json.dumps({"captainUrl": urls.gateway}, indent=2))
    return out.getvalue()


def desktop_zip(artifacts: Path, urls: Urls) -> bytes:
    src = artifacts / "desktop" / "windows"
    files = ["Invoke-CaptainDesktopAgent.ps1", "Register-CaptainDesktopAgent.ps1", "README.md"]
    if not all((src / f).exists() for f in files[:2]):
        raise ArtifactMissingError("the desktop agent scripts are missing")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            if (src / f).exists():
                z.writestr(f, (src / f).read_bytes())
        z.writestr("captain.json", json.dumps({"captainUrl": urls.gateway}, indent=2))
    return out.getvalue()


def claude_code_zip(artifacts: Path, urls: Urls) -> bytes:
    hook = artifacts / "ide" / "claude-code" / "captain_hook.py"
    if not hook.exists():
        raise ArtifactMissingError("the Claude Code hook is missing")
    settings = {
        "env": {"CAPTAIN_CORE_URL": urls.core, "CAPTAIN_KEY_FILE": "~/.captain/key"},
        "_how": "Merge 'env' into ~/.claude/settings.json (or managed settings), then run "
        "'python captain_hook.py --print-settings' and add the 'hooks' block it prints.",
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("captain_hook.py", hook.read_bytes())
        z.writestr("captain-settings-snippet.json", json.dumps(settings, indent=2))
    return out.getvalue()


def text_download(download_id: str, urls: Urls) -> bytes:
    """Small config files generated for this install. Keys are never included: each person
    or device uses its own Captain key."""
    mcp_header = {"Authorization": "Bearer ${env:CAPTAIN_AGENT_KEY}"}
    docs: dict[str, Any] = {
        "vscode-copilot-model": [{
            "name": "Stixis Captain", "vendor": "customendpoint", "apiType": "chat-completions",
            "apiKey": "<your Captain key>",
            "models": [{"id": "claude-sonnet", "name": "Captain", "toolCalling": True,
                        "url": f"{urls.gateway}/v1/chat/completions", "vision": False,
                        "maxInputTokens": 128000, "maxOutputTokens": 4096}],
        }],
        "mcp-vscode": {"servers": {"captain": {"type": "http", "url": urls.mcp,
                                              "headers": mcp_header}}},
        "mcp-claude-desktop": {"mcpServers": {"captain": {
            "command": "npx", "args": ["-y", "mcp-remote", urls.mcp, "--header",
                                       "Authorization: Bearer ${CAPTAIN_AGENT_KEY}"]}}},
        "mcp-cursor": {"mcpServers": {"captain": {
            "url": urls.mcp, "headers": {"Authorization": "Bearer ${env:CAPTAIN_AGENT_KEY}"}}}},
        "browser-policy": {"captainUrl": {"Value": urls.gateway},
                           "captainKey": {"Value": "<this user's Captain key>"},
                           "failMode": {"Value": "closed"}},
    }  # fmt: skip
    if download_id not in docs:
        raise KeyError(download_id)
    return json.dumps(docs[download_id], indent=2).encode()


def build_download(download_id: str, artifacts: Path, urls: Urls) -> tuple[bytes, str, str]:
    """(content, filename, media type)."""
    name = next(d.filename for a in CATALOG for d in a.downloads if d.id == download_id)
    if download_id == "vscode-extension":
        return vscode_vsix(artifacts, urls), name, "application/octet-stream"
    if download_id == "browser-extension":
        return browser_zip(artifacts, urls), name, "application/zip"
    if download_id == "desktop-agent":
        return desktop_zip(artifacts, urls), name, "application/zip"
    if download_id == "claude-code-hook":
        return claude_code_zip(artifacts, urls), name, "application/zip"
    return text_download(download_id, urls), name, "application/json"


def guide_text(guides: Path, name: str, urls: Urls) -> str:
    if not re.fullmatch(r"[a-z0-9-]{1,40}", name):
        raise KeyError(name)
    p = guides / f"{name}.md"
    if not p.exists():
        raise KeyError(name)
    return urls.fill(p.read_text(encoding="utf-8"))
