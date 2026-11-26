"""Claude Code hook (ide/claude-code/captain_hook.py) against the real Core HTTP API,
in process. The hook is a standalone stdlib file, loaded here by path."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from captain.api.core_http import create_app
from captain.core.guardrails.service import GuardrailService
from captain.core.identity.killswitch import KillScope
from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.models import AutonomyLevel, PrincipalType, RiskTier
from captain.core.registry.service import (
    InMemoryRegistryStore,
    McpServer,
    RegistryService,
    ToolEntry,
    ToolImpact,
    ToolRisk,
)
from tests.conftest import make_context
from tests.harness.adapters import CaptainCoreHarness
from tests.unit.test_engine import Guard

HOOK = Path(__file__).resolve().parents[3] / "ide" / "claude-code" / "captain_hook.py"
spec = importlib.util.spec_from_file_location("captain_hook", HOOK)
assert spec and spec.loader
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

ALLOWED = frozenset({"claudecode.Read", "claudecode.Bash", "claudecode.Edit"})


def registry() -> RegistryService:
    r = RegistryService(InMemoryRegistryStore())
    r.upsert_server(McpServer("claude-code-local", "local://claude-code", "owner-1"))
    for name, impact, risk, ro in [
        ("Read", ToolImpact.LOW, ToolRisk.LOW, True),
        ("Edit", ToolImpact.LOW, ToolRisk.MEDIUM, False),
        ("Bash", ToolImpact.HIGH, ToolRisk.HIGH, False),
        ("Write", ToolImpact.LOW, ToolRisk.MEDIUM, False),  # registered, not allowed
    ]:
        r.upsert_tool(
            ToolEntry(f"claudecode.{name}", "claude-code-local", name, impact, risk, read_only=ro)
        )
    return r


class Rig:
    def __init__(self, tmp: Path, autonomy: AutonomyLevel = AutonomyLevel.A2) -> None:
        self.h = CaptainCoreHarness()
        m = self.h.core.m
        m.registry = registry()
        m.guardrails = GuardrailService(Guard())
        ctx = make_context("dev-coder", RiskTier.TIER_3, autonomy, PrincipalType.USER)
        ctx = ctx.model_copy(
            update={
                "register_entry": ctx.register_entry.model_copy(update={"allowed_tools": ALLOWED})
            }
        )
        self.h.register_key("k", ctx)
        self.tc = TestClient(create_app(self.h.core))
        self.key = self.h._keys["k"]  # noqa: SLF001
        self.state = hook.State(tmp / "state.json")
        self.t = self.transport(self.key)

    def transport(self, key: str) -> Any:
        def call(method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
            r = self.tc.request(method, path, json=body, headers={"Authorization": f"Bearer {key}"})
            return r.status_code, r.json()

        return call

    def pre(self, tool: str, args: dict[str, Any] | None = None, t: Any = None, **kw: Any) -> Any:
        event = {
            "hook_event_name": "PreToolUse",
            "session_id": "sess-1234567890",
            "tool_name": tool,
            "tool_input": args if args is not None else {"file_path": "a.py"},
        }
        return hook.run(event, t or self.t, self.state, **kw)

    def ledger(self) -> list[tuple[str, dict[str, Any]]]:
        store = self.h.built.infra.ledger_store
        assert isinstance(store, InMemoryLedgerStore)
        return [(t, b) for (_p, t, b) in store.outbox.values()]


def reason(out: dict[str, Any]) -> str:
    return str(out["hookSpecificOutput"]["permissionDecisionReason"])


def is_deny(out: Any) -> bool:
    spec_out = (out or {}).get("hookSpecificOutput", {})
    return (
        spec_out.get("permissionDecision") == "deny" and spec_out["hookEventName"] == "PreToolUse"
    )


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


@pytest.fixture()
def rig_a3(tmp_path: Path) -> Rig:
    return Rig(tmp_path, AutonomyLevel.A3)


# ------------------------------------------------------------------- settings


def test_generated_settings_register_every_event() -> None:
    s = hook.settings('python "hook.py"', strict=False)["hooks"]
    assert set(s) == {"PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart"}
    assert "onFailure" not in json.dumps(s)
    strict = hook.settings('python "hook.py"', strict=True)["hooks"]
    assert strict["PreToolUse"][0]["hooks"][0]["onFailure"] == "block"
    assert "onFailure" not in strict["PostToolUse"][0]["hooks"][0]  # observation never blocks


# ----------------------------------------------------------------- PreToolUse


def test_allowed_call_is_silent_and_recorded(rig: Rig) -> None:
    assert rig.pre("Read") is None
    ev = [b for t, b in rig.ledger() if t == "call.allowed"]
    assert ev and ev[0]["tool"] == "claudecode.Read" and ev[0]["channel"] == "HOOK"


def test_refusals_are_denials_with_a_reason(rig: Rig) -> None:
    for tool, code in [("Glob", "TOOL_NOT_REGISTERED"), ("Write", "TOOL_NOT_ALLOWED")]:
        out = rig.pre(tool)
        assert is_deny(out) and code in reason(out), tool
    rig.h.core.kill(KillScope.TOOL, "claudecode.Read", "stop", "admin")
    assert "KILL_SWITCH_ACTIVE" in reason(rig.pre("Read"))
    assert is_deny(hook.run({"hook_event_name": "PreToolUse"}, rig.t, rig.state))  # unreadable call


def test_approval_flow_holds_then_allows_once(rig_a3: Rig) -> None:
    r = rig_a3
    args = {"command": "rm -rf build"}
    held = r.pre("Bash", args)
    assert is_deny(held) and "ticket" in reason(held)
    ticket = reason(held).split("ticket ")[1].split(")")[0]
    assert "Still waiting" in reason(r.pre("Bash", args))  # same call, undecided
    r.h.decide_approval(UUID(ticket), True, "fin-1")
    assert r.pre("Bash", args) is None  # approved: runs, and the approval is used
    again = r.pre("Bash", args)  # a second run needs a fresh approval
    assert is_deny(again) and ticket not in reason(again)
    other = r.pre("Bash", {"command": "rm -rf src"})  # different arguments: different ticket
    assert is_deny(other) and "ticket" in reason(other)


def test_rejected_approval_is_final_for_that_ticket(rig_a3: Rig) -> None:
    r = rig_a3
    held = r.pre("Bash", {"command": "x"})
    ticket = reason(held).split("ticket ")[1].split(")")[0]
    r.h.core.decide_approval(UUID(ticket), False, "fin-1", "no")
    out = r.pre("Bash", {"command": "x"})
    assert is_deny(out) and "rejected" in reason(out)


def test_captain_unreachable_fails_open_unless_set_to_fail_closed(rig: Rig) -> None:
    def down(*_: Any) -> Any:
        raise ConnectionRefusedError

    assert rig.pre("Read", t=down) is None
    assert is_deny(rig.pre("Read", t=down, fail_closed=True))


def test_a_rejected_key_blocks(rig: Rig) -> None:
    out = rig.pre("Read", t=rig.transport("cpt_live_wrong"))
    assert is_deny(out)


# ---------------------------------------------------------------- observation


def test_observation_events_record_no_content(rig: Rig) -> None:
    secret_prompt = "please use password hunter2-private for the deploy"
    for event in [
        {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": secret_prompt},
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "Bash",
            "duration_ms": 42,
            "tool_input": {"command": "echo hi"},
            "tool_response": {"stdout": "hi-output"},
        },
        {"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup"},
    ]:
        assert hook.run(event, rig.t, rig.state) is None
    ledger = rig.ledger()
    observed = [b for t, b in ledger if b.get("coverage") == "OBSERVED"]
    assert len(observed) == 3
    kinds = {b["details"]["hook_event"] for b in observed}
    assert kinds == {"UserPromptSubmit", "PostToolUse", "SessionStart"}
    prompt = next(b for b in observed if b["details"]["hook_event"] == "UserPromptSubmit")
    assert prompt["details"]["prompt_chars"] == len(secret_prompt)
    blob = json.dumps(ledger)
    assert "hunter2" not in blob and "hi-output" not in blob and "echo hi" not in blob


def test_observation_failure_never_blocks(rig: Rig) -> None:
    def down(*_: Any) -> Any:
        raise TimeoutError

    assert (
        hook.run({"hook_event_name": "UserPromptSubmit", "prompt": "x"}, down, rig.state, True)
        is None
    )


# ------------------------------------------------------------------ transport


def test_core_url_must_be_https_or_loopback() -> None:
    assert hook.check_url("https://captain.example.com/") == "https://captain.example.com"
    assert hook.check_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    for bad in ["http://captain.example.com", "ftp://x", "https://u:p@captain.example.com", "x"]:
        with pytest.raises(ValueError):
            hook.check_url(bad)
