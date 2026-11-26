"""Upstream MCP client: Captain calls a registered MCP server on the caller's behalf.

Two protocol eras are supported, because servers in the field are in both:
- modern (2026-07-28): stateless. The server is probed once with `server/discover`;
  tool calls then carry no session.
- legacy (2025 revisions): `initialize` → `notifications/initialized` → `tools/call`,
  with the `Mcp-Session-Id` the server returns, and a best-effort session close.
The era of each server is detected on first use and cached. A server that does not
answer `server/discover` with a result is treated as legacy.

Safety (Master Prompt §security): the URL is checked before every call (SSRF guard),
redirects are never followed, the response size is capped, there are timeouts, the
credential comes from a SecretResolver at call time and is never logged.
DNS rebinding: every request resolves the host once, validates all of its addresses, and
connects to that exact address (Host header and TLS name kept), so a server cannot switch
addresses between the check and the connection. An egress firewall that allows only the
registered MCP hosts is still recommended as a second layer.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import socket
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from captain.core.models import ToolTarget
from captain.integrations.api_gateway.provider import SecretResolver

MODERN_VERSION = "2026-07-28"
LEGACY_VERSION = "2025-06-18"
MAX_RESPONSE_BYTES = 1_000_000


class UpstreamError(Exception):
    """The upstream server could not be reached, refused, or sent something unusable."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UnsafeUrlError(UpstreamError):
    pass


class UpstreamRpcError(UpstreamError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"upstream error {code}: {message[:200]}")
        self.code = code


@dataclass(frozen=True, slots=True)
class UpstreamResult:
    content: list[dict[str, Any]]
    is_error: bool


# ------------------------------------------------------------------ SSRF guard


def _validated(
    url: str, allow_loopback: bool, allow_private: bool
) -> tuple[Any, list[ipaddress.IPv4Address | ipaddress.IPv6Address]]:
    """Resolve the host ONCE and check every address it resolved to. Returns the parsed URL
    and the addresses (in resolver order)."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host or parts.scheme not in ("http", "https") or parts.username or parts.password:
        raise UnsafeUrlError("MCP server URL must be http(s) without embedded credentials")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise UnsafeUrlError(f"MCP server host does not resolve: {host}") from e
    ordered: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for i in infos:
        a = ipaddress.ip_address(i[4][0].split("%")[0])
        if a not in ordered:
            ordered.append(a)
    all_loopback = all(a.is_loopback for a in ordered)
    if parts.scheme == "http" and not (allow_loopback and all_loopback):
        raise UnsafeUrlError("MCP server URL must use https")
    for a in ordered:
        if a.is_link_local or a.is_unspecified or a.is_multicast or a.is_reserved:
            raise UnsafeUrlError(f"MCP server address {a} is not allowed")
        if a.is_loopback and not allow_loopback:
            raise UnsafeUrlError("MCP server on a loopback address is not allowed")
        if a.is_private and not a.is_loopback and not allow_private:
            raise UnsafeUrlError(f"MCP server address {a} is private")
    return parts, ordered


def check_url(url: str, allow_loopback: bool = False, allow_private: bool = True) -> None:
    """Refuse URLs an MCP server should never have. https only; http only to loopback
    in development. Cloud metadata / link-local, unspecified, multicast and reserved
    addresses are always refused; so is loopback unless explicitly allowed."""
    _validated(url, allow_loopback, allow_private)


def pinned(
    url: str, allow_loopback: bool, allow_private: bool
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """The URL to actually connect to, closing the DNS-rebinding gap: the host is resolved
    once, validated, and the connection goes to THAT address. The original host name is kept
    for the Host header and for TLS (certificate name and SNI), so https still verifies the
    real server. Returns (url, extra headers, request extensions)."""
    parts, addrs = _validated(url, allow_loopback, allow_private)
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
        return url, {}, {}  # already a literal address: nothing to pin
    except ValueError:
        pass
    ip = addrs[0]
    literal = f"[{ip}]" if ip.version == 6 else str(ip)
    port = f":{parts.port}" if parts.port else ""
    pinned_url = parts._replace(netloc=f"{literal}{port}").geturl()
    headers = {"Host": f"{host}{port}"}
    ext: dict[str, Any] = {"sni_hostname": host} if parts.scheme == "https" else {}
    return pinned_url, headers, ext


# ---------------------------------------------------------------------- client


def _header_value(value: str) -> str:
    """Header-safe form of a name (printable ASCII as is, otherwise base64 in the
    `=?base64?…?=` wrapper the 2026 spec uses)."""
    if value.isascii() and value.isprintable() and value == value.strip():
        return value
    return "=?base64?" + base64.b64encode(value.encode()).decode("ascii") + "?="


def _parse_sse(text: str, want_id: str) -> dict[str, Any] | None:
    """Return the JSON-RPC message answering `want_id` from an event stream body."""
    data: list[str] = []
    found: dict[str, Any] | None = None
    for line in [*text.splitlines(), ""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "" and data:
            try:
                msg = json.loads("\n".join(data))
            except ValueError:
                msg = None
            data = []
            if isinstance(msg, dict) and msg.get("id") == want_id:
                found = msg
    return found


class McpUpstream:
    def __init__(
        self,
        secrets: SecretResolver,
        *,
        allow_loopback: bool = False,
        allow_private: bool = True,
        timeout_s: float = 30.0,
        connect_timeout_s: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._secrets = secrets
        self._allow_loopback = allow_loopback
        self._allow_private = allow_private
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
            follow_redirects=False,
            transport=transport,
        )
        self._era: dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- transport
    def _post(
        self, url: str, headers: dict[str, str], body: dict[str, Any], expect_reply: bool = True
    ) -> tuple[httpx.Headers, dict[str, Any] | None]:
        pin_url, pin_headers, pin_ext = pinned(url, self._allow_loopback, self._allow_private)
        try:
            with self._client.stream(
                "POST",
                pin_url,
                headers={**headers, **pin_headers},
                json=body,
                extensions=pin_ext,
            ) as r:
                chunks, size = [], 0
                for part in r.iter_bytes():
                    size += len(part)
                    if size > MAX_RESPONSE_BYTES:
                        raise UpstreamError("upstream response is too large")
                    chunks.append(part)
                raw = b"".join(chunks)
                status, resp_headers = r.status_code, r.headers
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise UpstreamError(f"upstream unreachable: {type(e).__name__}") from e
        if status >= 400:
            raise UpstreamError(f"upstream HTTP {status}", status)
        if not expect_reply or not raw.strip():
            return resp_headers, None
        text = raw.decode("utf-8", errors="replace")
        ctype = resp_headers.get("content-type", "")
        try:
            if "text/event-stream" in ctype:
                msg = _parse_sse(text, str(body.get("id")))
            else:
                msg = json.loads(text)
        except ValueError as e:
            raise UpstreamError("upstream sent invalid JSON") from e
        if not isinstance(msg, dict):
            raise UpstreamError("upstream sent no usable reply")
        err = msg.get("error")
        if isinstance(err, dict):
            raise UpstreamRpcError(int(err.get("code", -32603)), str(err.get("message", "")))
        return resp_headers, msg

    @staticmethod
    def _rpc(
        method: str, params: dict[str, Any] | None = None, notify: bool = False
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = uuid4().hex
        return msg

    def _modern(
        self, target: ToolTarget, method: str, params: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """Headers and body of a 2026-07-28 request: each request is self-contained, so the
        protocol version, client info and capabilities travel in params._meta (shapes
        checked against the official SDK's client)."""
        meta = {
            "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
            "io.modelcontextprotocol/clientInfo": {"name": "stixis-captain", "version": "0.1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        headers = {**self._headers(target, MODERN_VERSION), "Mcp-Method": method}
        name = params.get("name")
        if method == "tools/call" and isinstance(name, str):
            headers["Mcp-Name"] = _header_value(name)
        return headers, self._rpc(method, {**params, "_meta": meta})

    def _headers(self, target: ToolTarget, version: str) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": version,
        }
        if target.credential_ref:
            h["Authorization"] = f"Bearer {self._secrets.secret(target.credential_ref)}"
        return h

    # ------------------------------------------------------------------ era
    def _detect(self, target: ToolTarget) -> str:
        with self._lock:
            known = self._era.get(target.server_id)
        if known:
            return known
        era = "legacy"
        try:
            headers, body = self._modern(target, "server/discover", {})
            _, msg = self._post(target.url, headers, body)
            if msg is not None and isinstance(msg.get("result"), dict):
                era = "modern"
        except UpstreamRpcError:
            pass  # method not found etc.: an older server
        except UpstreamError as e:
            if e.status not in (400, 404, 405, 406, 415):
                raise  # unreachable, unauthorised or failing is not "legacy"
        with self._lock:
            self._era[target.server_id] = era
        return era

    # ----------------------------------------------------------------- calls
    def call_tool(self, target: ToolTarget, arguments: dict[str, Any]) -> UpstreamResult:
        check_url(target.url, self._allow_loopback, self._allow_private)
        era = self._detect(target)
        params = {"name": target.upstream_name, "arguments": arguments}
        if era == "modern":
            headers, body = self._modern(target, "tools/call", params)
            _, msg = self._post(target.url, headers, body)
            return self._result(msg)
        return self._legacy_call(target, params)

    def _legacy_call(self, target: ToolTarget, params: dict[str, Any]) -> UpstreamResult:
        h = self._headers(target, LEGACY_VERSION)
        init = self._rpc(
            "initialize",
            {
                "protocolVersion": LEGACY_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "stixis-captain", "version": "0.1"},
            },
        )
        resp_headers, _ = self._post(target.url, h, init)
        sid = resp_headers.get("mcp-session-id")
        if sid:
            h = {**h, "Mcp-Session-Id": sid}
        try:
            self._post(target.url, h, self._rpc("notifications/initialized", notify=True), False)
            _, msg = self._post(target.url, h, self._rpc("tools/call", params))
            return self._result(msg)
        finally:
            if sid:
                try:
                    self._client.delete(target.url, headers=h)
                except httpx.HTTPError:
                    pass  # best effort

    @staticmethod
    def _result(msg: dict[str, Any] | None) -> UpstreamResult:
        res = (msg or {}).get("result")
        if not isinstance(res, dict) or not isinstance(res.get("content", []), list):
            raise UpstreamError("upstream tool result is malformed")
        content = [c for c in res.get("content", []) if isinstance(c, dict)]
        return UpstreamResult(content=content, is_error=bool(res.get("isError", False)))
