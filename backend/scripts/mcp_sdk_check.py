"""Official MCP SDK talking to a running Captain MCP proxy (compatibility check).

Install the SDK in a SEPARATE virtual environment (it pins other versions of pydantic and
starlette than the Captain backend): `python -m venv sdkenv; sdkenv/Scripts/pip install mcp`.

    python mcp_sdk_check.py <mode> hold              # list tools, read call, a write call -> ticket
    python mcp_sdk_check.py <mode> retry <ticket>    # repeat the write with the approved ticket
"""

import asyncio
import sys
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

KEY = (Path(__file__).resolve().parents[1] / ".dev" / "dev-agent.key").read_text().strip()
URL = "http://127.0.0.1:8300/mcp"
APPROVAL = "com.stixis.captain/approval_ticket"
ARGS = {"title": "Rotate the signing key"}


def show(label: str, value: object) -> None:
    print(f"{label:<12}: {value}")


async def main(mode: str, step: str, ticket: str | None) -> None:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {KEY}"})
    transport = streamable_http_client(URL, http_client=http)
    async with Client(transport, mode=mode) as client:
        show("protocol", getattr(client, "protocol_version", "?"))
        tools = await client.list_tools()
        show("tools", [t.name for t in tools.tools])
        if step == "hold":
            r = await client.call_tool("demo.search_issues", {"query": "login bug"})
            show("read", (r.is_error, r.content[0].text))
            r = await client.call_tool("demo.create_issue", ARGS)
            show("write", (r.is_error, (r.meta or {}).get(APPROVAL)))
            r = await client.call_tool("demo.admin_reset", {})
            show("not allowed", (r.is_error, r.content[0].text[:90]))
        else:
            r = await client.call_tool("demo.create_issue", ARGS, meta={APPROVAL: ticket})
            show("approved", (r.is_error, r.content[0].text))
            r = await client.call_tool("demo.create_issue", ARGS, meta={APPROVAL: ticket})
            show("replay", (r.is_error, r.content[0].text[:80]))


asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
