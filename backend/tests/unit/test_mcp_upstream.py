"""Upstream MCP client against the mock MCP server (both protocol eras), plus the
SSRF guard, size cap and SSE parsing."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from captain.core.models import ToolTarget
from captain.devmcp import create_dev_mcp
from captain.integrations.mcp_proxy.upstream import (
    McpUpstream,
    UnsafeUrlError,
    UpstreamError,
    _parse_sse,
    check_url,
)

URL = "http://127.0.0.1:8310/mcp"


class Secrets:
    def __init__(self, value: str = "mock-mcp-key") -> None:
        self.value = value

    def secret(self, ref: str) -> str:
        return self.value


def bridge(tc: TestClient, seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    """Route the client's requests into the mock server's ASGI app."""

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        r = tc.request(req.method, req.url.path, headers=dict(req.headers), content=req.content)
        return httpx.Response(r.status_code, headers=dict(r.headers), content=r.content)

    return httpx.MockTransport(handler)


def target(name: str = "search_issues", ref: str | None = "env:K", url: str = URL) -> ToolTarget:
    return ToolTarget(
        tool=f"demo.{name}",
        server_id="demo-mcp",
        upstream_name=name,
        url=url,
        transport="streamable_http",
        credential_ref=ref,
    )


def client(mode: str, secrets: Secrets | None = None) -> tuple[McpUpstream, TestClient, list]:  # type: ignore[type-arg]
    app = create_dev_mcp(mode)
    tc = TestClient(app)
    seen: list[httpx.Request] = []
    up = McpUpstream(secrets or Secrets(), allow_loopback=True, transport=bridge(tc, seen))
    return up, tc, seen


@pytest.mark.parametrize("mode", ["modern", "legacy"])
def test_call_works_in_both_eras(mode: str) -> None:
    up, tc, _ = client(mode)
    res = up.call_tool(target(), {"query": "bugs"})
    assert not res.is_error and "CAP-1" in res.content[0]["text"]
    assert tc.app.state.calls == [("search_issues", {"query": "bugs"})]  # type: ignore[attr-defined]


def test_era_is_detected_once_and_cached() -> None:
    up, _, seen = client("modern")
    up.call_tool(target(), {})
    up.call_tool(target(), {})
    methods = [r.content.decode() for r in seen]
    assert sum('"server/discover"' in m for m in methods) == 1
    assert not any('"initialize"' in m for m in methods)


def test_legacy_uses_a_session_and_closes_it() -> None:
    up, _, seen = client("legacy")
    up.call_tool(target(), {})
    methods = [r.method for r in seen]
    assert methods[-1] == "DELETE"  # session closed
    calls = [r for r in seen if r.method == "POST" and b"tools/call" in r.content]
    assert calls and all("mcp-session-id" in r.headers for r in calls)


def test_credential_is_sent_as_bearer_and_wrong_one_fails() -> None:
    up, _, seen = client("modern")
    up.call_tool(target(), {})
    assert all(r.headers["authorization"] == "Bearer mock-mcp-key" for r in seen)
    bad, _, _ = client("modern", Secrets("wrong"))
    with pytest.raises(UpstreamError):
        bad.call_tool(target(), {})


def test_tool_error_flag_is_passed_through() -> None:
    up, _, _ = client("modern")
    res = up.call_tool(target("does_not_exist"), {})
    assert res.is_error


def test_non_text_content_is_returned_for_the_caller_to_handle() -> None:
    up, _, _ = client("modern")
    res = up.call_tool(target("image"), {})
    assert res.content[0]["type"] == "image"


def test_oversized_response_is_refused() -> None:
    up, _, _ = client("modern")
    with pytest.raises(UpstreamError, match="too large"):
        up.call_tool(target("big"), {})


def test_unreachable_server_is_an_error_not_legacy() -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    up = McpUpstream(Secrets(), allow_loopback=True, transport=httpx.MockTransport(boom))
    with pytest.raises(UpstreamError, match="unreachable"):
        up.call_tool(target(), {})


def test_redirects_are_not_followed() -> None:
    def redirect(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    up = McpUpstream(Secrets(), allow_loopback=True, transport=httpx.MockTransport(redirect))
    # a 3xx is not a success; era detection treats it as "not modern", the call then fails
    with pytest.raises(UpstreamError):
        up.call_tool(target(), {})


def test_sse_reply_is_parsed() -> None:
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":"a","result":{"x":1}}\n\n'
    assert _parse_sse(body, "a") == {"jsonrpc": "2.0", "id": "a", "result": {"x": 1}}
    assert _parse_sse(body, "other") is None
    assert _parse_sse("data: not json\n\n", "a") is None


def test_sse_response_over_http() -> None:
    def sse(req: httpx.Request) -> httpx.Response:
        import json

        msg = json.loads(req.content)
        if msg["method"] == "server/discover":
            result = {"supportedVersions": ["2026-07-28"]}
        else:
            result = {"content": [{"type": "text", "text": "via sse"}], "isError": False}
        payload = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        data = f"event: message\ndata: {payload}\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)

    up = McpUpstream(Secrets(), allow_loopback=True, transport=httpx.MockTransport(sse))
    assert up.call_tool(target(), {}).content[0]["text"] == "via sse"


# ----------------------------------------------------------------- SSRF guard


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://169.254.169.254/",
        "https://0.0.0.0/mcp",
        "https://224.0.0.1/mcp",
        "https://[fe80::1]/mcp",
        "ftp://example.com/mcp",
        "file:///etc/passwd",
        "https://user:pass@example.com/mcp",
        "http://203.0.113.10/mcp",  # plain http to a non-loopback host
        "https:///mcp",
    ],
)
def test_ssrf_guard_refuses_dangerous_urls(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        check_url(url, allow_loopback=True)


def test_loopback_only_when_allowed_and_only_for_http_dev() -> None:
    check_url("http://127.0.0.1:8310/mcp", allow_loopback=True)
    with pytest.raises(UnsafeUrlError):
        check_url("http://127.0.0.1:8310/mcp", allow_loopback=False)
    with pytest.raises(UnsafeUrlError):
        check_url("https://127.0.0.1/mcp", allow_loopback=False)


def test_private_ranges_can_be_refused() -> None:
    check_url("https://10.1.2.3/mcp", allow_private=True)
    with pytest.raises(UnsafeUrlError):
        check_url("https://10.1.2.3/mcp", allow_private=False)


def test_call_checks_the_url_before_any_request() -> None:
    seen: list[httpx.Request] = []
    up = McpUpstream(
        Secrets(),
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)),  # type: ignore[func-returns-value]
    )
    with pytest.raises(UnsafeUrlError):
        up.call_tool(target(url="http://169.254.169.254/mcp"), {})
    assert seen == []


# ------------------------------------------------------------ DNS rebinding


def _answers(*ips: str) -> Any:
    """A getaddrinfo that returns the given addresses one lookup at a time (then repeats the
    last): models a hostile DNS server that changes its answer."""
    calls = {"n": 0}

    def fake(host: str, port: int, *a: Any, **k: Any) -> list[Any]:
        ip = ips[min(calls["n"], len(ips) - 1)]
        calls["n"] += 1
        fam = 10 if ":" in ip else 2
        return [(fam, 1, 6, "", (ip, port))]

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


def test_the_connection_goes_to_the_validated_address_with_the_original_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from captain.integrations.mcp_proxy import upstream as U

    fake = _answers("93.184.216.34")
    monkeypatch.setattr(U.socket, "getaddrinfo", fake)
    url, headers, ext = U.pinned("https://mcp.example.com:8443/mcp?x=1", False, True)
    assert url == "https://93.184.216.34:8443/mcp?x=1"
    assert headers == {"Host": "mcp.example.com:8443"}
    assert ext == {"sni_hostname": "mcp.example.com"}  # TLS still verifies the real name
    assert fake.calls["n"] == 1  # resolved once
    v6 = _answers("2001:db8::1")
    monkeypatch.setattr(U.socket, "getaddrinfo", v6)
    assert U.pinned("https://m.example.com/mcp", False, True)[0] == "https://[2001:db8::1]/mcp"
    assert U.pinned("https://93.184.216.34/mcp", False, True) == (
        "https://93.184.216.34/mcp",
        {},
        {},
    )


def test_a_host_that_changes_its_answer_after_the_check_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classic rebinding: the first lookup is a public address (passes the check), the next
    is the cloud metadata address. The request itself resolves and validates again, so it
    never connects to the second answer."""
    from captain.integrations.mcp_proxy import upstream as U

    monkeypatch.setattr(U.socket, "getaddrinfo", _answers("93.184.216.34", "169.254.169.254"))
    seen: list[httpx.Request] = []
    up = McpUpstream(
        Secrets(),
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)),  # type: ignore[func-returns-value]
    )
    with pytest.raises(UnsafeUrlError):
        up.call_tool(target(url="https://mcp.example.com/mcp"), {})
    assert seen == []  # no request was ever made to the metadata address


def test_a_mixed_answer_with_any_bad_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from captain.integrations.mcp_proxy import upstream as U

    def mixed(host: str, port: int, *a: Any, **k: Any) -> list[Any]:
        return [(2, 1, 6, "", ("93.184.216.34", port)), (2, 1, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr(U.socket, "getaddrinfo", mixed)
    with pytest.raises(UnsafeUrlError):
        U.pinned("https://mcp.example.com/mcp", False, True)
