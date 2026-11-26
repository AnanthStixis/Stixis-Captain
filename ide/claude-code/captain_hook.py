#!/usr/bin/env python3
"""Stixis Captain hook for Claude Code. Standard library only: copy this one file.

Claude Code runs it for hook events and passes the event as JSON on stdin.

  PreToolUse        asks Captain whether this tool call is allowed, BEFORE it runs.
                    Captain can allow, refuse, or hold it for human approval.
  PostToolUse       reports that a tool ran (observation; no tool output is sent).
  UserPromptSubmit  reports that a prompt was submitted (observation; the prompt text
                    is NOT sent, only its length).
  SessionStart      reports that a session started (observation).

What this does and does not give you (be honest with auditors):
- PreToolUse decisions are enforced only while this hook is installed and runs. A
  developer can remove a user-level hook; install it in Claude Code's managed settings
  (with `allowManagedHooksOnly`) to prevent that.
- By default a hook failure lets the action proceed (Claude Code's own default). Use
  `--strict` when generating settings to make Claude Code block on hook failure, or set
  CAPTAIN_HOOK_FAIL_CLOSED=1 so this script refuses when Captain cannot be reached.
- Observation events record that something happened. They cannot prevent it.

Settings (environment):
  CAPTAIN_CORE_URL   Captain Core API base URL (default http://127.0.0.1:8080)
  CAPTAIN_KEY        the developer's Captain key, or
  CAPTAIN_KEY_FILE   a file holding it (default ~/.captain/key)
  CAPTAIN_STATE_FILE where pending approvals are remembered (default ~/.captain/hook-state.json)

    python captain_hook.py --print-settings [--strict]   # settings.json hooks block
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

TOOL_PREFIX = "claudecode."
OBSERVED_EVENTS = {"PostToolUse", "UserPromptSubmit", "SessionStart"}
STATE_TTL_S = 3600
LOOPBACK = {"localhost", "127.0.0.1", "::1"}

# (method, path, body) -> (HTTP status, parsed JSON or None). Raises OSError if unreachable.
Transport = Callable[[str, str, dict[str, Any] | None], tuple[int, Any]]


# ----------------------------------------------------------------- transport
def check_url(url: str) -> str:
    """The key is a bearer secret: https only, except for a Captain on this machine."""
    p = urlsplit(url)
    if p.username or p.password or not p.hostname:
        raise ValueError("CAPTAIN_CORE_URL must not contain credentials")
    if p.scheme == "https" or (p.scheme == "http" and p.hostname in LOOPBACK):
        return url.rstrip("/")
    raise ValueError("CAPTAIN_CORE_URL must use https (http is only allowed for this machine)")


def http_transport(base: str, key: str, timeout: float = 5.0) -> Transport:
    base = check_url(base)

    def call(method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 - scheme checked by check_url
            base + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                raw = r.read(1_000_000)
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read(100_000) or b"null")
            except ValueError:
                return e.code, None

    return call


# --------------------------------------------------------------------- state
def call_key(tool: str, args: dict[str, Any]) -> str:
    canon = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{tool}|{canon}".encode()).hexdigest()


class State:
    """Remembers which approval ticket belongs to which exact tool call."""

    def __init__(self, path: Path) -> None:
        self._p = path

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        now = time.time()
        return {k: v for k, v in data.items() if now - v.get("at", 0) < STATE_TTL_S}

    def get(self, key: str) -> dict[str, Any] | None:
        return self._load().get(key)

    def put(self, key: str, ticket: str, request_id: str) -> None:
        data = self._load()
        data[key] = {"ticket": ticket, "request_id": request_id, "at": time.time()}
        self._p.parent.mkdir(parents=True, exist_ok=True)
        self._p.write_text(json.dumps(data), encoding="utf-8")

    def drop(self, key: str) -> None:
        data = self._load()
        if data.pop(key, None) is not None:
            self._p.write_text(json.dumps(data), encoding="utf-8")


# -------------------------------------------------------------------- output
def deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


class Unavailable(Exception):
    """Captain could not be reached or answered with a server error."""


def _post(t: Transport, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    try:
        status, data = t("POST", path, body)
    except OSError as e:
        raise Unavailable(type(e).__name__) from e
    if status >= 500:
        raise Unavailable(f"HTTP {status}")
    return status, data


def _reason(data: Any) -> str:
    return (
        str(data.get("explanation") or data.get("message") or "") if isinstance(data, dict) else ""
    )


# ------------------------------------------------------------------ handlers
def pre_tool_use(event: dict[str, Any], t: Transport, state: State) -> dict[str, Any] | None:
    name = str(event.get("tool_name") or "")
    args = event.get("tool_input")
    if not name or not isinstance(args, dict):
        return deny("Captain could not read this tool call.")
    tool = TOOL_PREFIX + name
    ck = call_key(tool, args)

    held = state.get(ck)
    if held:  # this exact call was held earlier: has the approver decided?
        status, tk = t("GET", f"/core/v1/approvals/{held['ticket']}", None)
        if status == 200 and tk.get("state") == "APPROVED":
            cstatus, cdata = _post(
                t,
                f"/core/v1/approvals/{held['ticket']}/consume",
                {"request_id": held["request_id"]},
            )
            state.drop(ck)
            if cstatus == 200:
                return None  # approved, used once
            return deny(f"Captain approval could not be used: {_reason(cdata)}")
        if status == 200 and tk.get("state") == "PENDING":
            return deny(
                f"Still waiting for approval (ticket {held['ticket']}). Retry after it is approved."
            )
        state.drop(ck)  # rejected, expired, used, or unknown: start over below
        if status == 200:
            return deny(f"Approval {str(tk.get('state')).lower()}; this call will not run.")

    status, d = _post(
        t,
        "/core/v1/decide",
        {
            "channel": "HOOK",
            "action": "TOOL_CALL",
            "tool": tool,
            "tool_arguments": args,
            "contents": [{"text": json.dumps(args, sort_keys=True, default=str), "trusted": True}],
            "trace_id": f"cc-{str(event.get('session_id', ''))[:16]}",
        },
    )
    if status in (401, 403):
        return deny(f"Captain refused this tool call: {_reason(d) or 'not authorised'}")
    if status != 200 or not isinstance(d, dict):
        return deny("Captain could not decide this tool call.")
    decision = d.get("decision")
    if decision in ("ALLOW", "ALLOW_WITH_LIMITS", "OBSERVE_ONLY"):
        return None
    if decision == "REQUIRE_APPROVAL":
        astatus, tk = _post(t, "/core/v1/approvals", {"request_id": d["request_id"]})
        if astatus != 200:
            return deny(f"Captain could not request approval: {_reason(tk)}")
        state.put(ck, tk["ticket_id"], d["request_id"])
        return deny(
            f"This action needs human approval in Captain (ticket {tk['ticket_id']}). Ask an "
            "approver to approve it in the Captain console, then retry the same action."
        )
    # BLOCK, and ALLOW_WITH_REDACTION (editing a tool call in flight is not supported)
    why = _reason(d) or str(d.get("reason_code"))
    if decision == "ALLOW_WITH_REDACTION":
        why = "This tool call contains data Captain would mask; remove it and retry."
    return deny(f"Blocked by Captain policy ({d.get('reason_code')}): {why}")


def observe(event: dict[str, Any], t: Transport) -> None:
    kind = str(event.get("hook_event_name"))
    details: dict[str, str | int | float | bool | None] = {"hook_event": kind}
    etype = "call.observed"
    tool = None
    if kind == "PostToolUse":
        etype, tool = "tool.executed", TOOL_PREFIX + str(event.get("tool_name") or "unknown")
        details["duration_ms"] = (
            event.get("duration_ms") if isinstance(event.get("duration_ms"), int) else None
        )
    elif kind == "UserPromptSubmit":
        details["prompt_chars"] = len(str(event.get("prompt") or ""))
    elif kind == "SessionStart":
        details["source"] = str(event.get("source") or "")
    t(
        "POST",
        "/core/v1/record",
        {
            "event_type": etype,
            "channel": "HOOK",
            "coverage": "OBSERVED",
            "trace_id": f"cc-{str(event.get('session_id', ''))[:16]}",
            "tool": tool,
            "details": details,
        },
    )


def run(
    event: dict[str, Any], t: Transport, state: State, fail_closed: bool = False
) -> dict[str, Any] | None:
    """Returns the JSON to print on stdout (or None). Never raises for operational problems."""
    kind = event.get("hook_event_name")
    try:
        if kind == "PreToolUse":
            return pre_tool_use(event, t, state)
        if kind in OBSERVED_EVENTS:
            observe(event, t)
        return None
    except (Unavailable, OSError, KeyError, ValueError) as e:
        print(f"captain hook: {type(e).__name__}: {e}", file=sys.stderr)
        if kind == "PreToolUse" and fail_closed:
            return deny("Captain is unreachable and this machine is set to fail closed.")
        return None


def settings(command: str, strict: bool) -> dict[str, Any]:
    def entry(timeout: int) -> dict[str, Any]:
        h: dict[str, Any] = {"type": "command", "command": command, "timeout": timeout}
        if strict:
            h["onFailure"] = "block"
        return {"matcher": "*", "hooks": [h]}

    obs = {"type": "command", "command": command, "timeout": 10}
    return {
        "hooks": {
            "PreToolUse": [entry(20)],
            "PostToolUse": [{"matcher": "*", "hooks": [obs]}],
            "UserPromptSubmit": [{"hooks": [obs]}],
            "SessionStart": [{"hooks": [obs]}],
        }
    }


def main(argv: list[str]) -> int:
    if "--print-settings" in argv:
        cmd = f'python "{Path(__file__).resolve()}"'
        print(json.dumps(settings(cmd, "--strict" in argv), indent=2))
        return 0
    home = Path.home() / ".captain"
    key = os.environ.get("CAPTAIN_KEY") or ""
    if not key:
        try:
            key = Path(os.environ.get("CAPTAIN_KEY_FILE", str(home / "key"))).read_text().strip()
        except OSError:
            key = ""
    fail_closed = os.environ.get("CAPTAIN_HOOK_FAIL_CLOSED") == "1"
    try:
        event = json.loads(sys.stdin.read())
        if not isinstance(event, dict):
            raise ValueError("event is not an object")
    except ValueError:
        print("captain hook: unreadable event", file=sys.stderr)
        return 0
    if not key:
        print("captain hook: no CAPTAIN_KEY configured", file=sys.stderr)
        out = (
            deny("No Captain key is configured on this machine.")
            if (fail_closed and event.get("hook_event_name") == "PreToolUse")
            else None
        )
    else:
        try:
            t = http_transport(os.environ.get("CAPTAIN_CORE_URL", "http://127.0.0.1:8080"), key)
        except ValueError as e:
            print(f"captain hook: {e}", file=sys.stderr)
            return 0
        state = State(Path(os.environ.get("CAPTAIN_STATE_FILE", str(home / "hook-state.json"))))
        out = run(event, t, state, fail_closed)
    if out is not None:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
