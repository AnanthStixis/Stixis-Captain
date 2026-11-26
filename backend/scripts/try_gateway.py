"""Try the Captain API Gateway with the standard OpenAI SDK (local development).

    .venv\\Scripts\\python scripts\\try_gateway.py

Needs the services from the README running (OPA, worker, mock provider, gateway) and
the dev key from `python -m captain.devseed` in backend/.dev/dev-app.key.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
from openai import APIStatusError, OpenAI

BASE = "http://127.0.0.1:8000"
KEY = (Path(__file__).resolve().parents[1] / ".dev" / "dev-app.key").read_text().strip()
client = OpenAI(base_url=f"{BASE}/v1", api_key=KEY, max_retries=0)
auth = {"Authorization": f"Bearer {KEY}"}


def show(label: str, text: str) -> None:
    print(f"{label:<14}: {text}")


raw = client.chat.completions.with_raw_response.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "Summarise our refund policy"}],
    max_tokens=100,
)
c = raw.parse()
show(
    "1 normal",
    f"{c.choices[0].message.content!r} | tokens {c.usage.total_tokens if c.usage else '?'} "
    f"| decision {raw.headers.get('x-captain-decision')} "
    f"| risk {raw.headers.get('x-captain-risk')}",
)

text, usage = "", None
for ch in client.chat.completions.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "stream this"}],
    stream=True,
    stream_options={"include_usage": True},
):
    if ch.choices:
        text += ch.choices[0].delta.content or ""
    if ch.usage:
        usage = ch.usage
show("2 streaming", f"{text!r} | usage {usage.total_tokens if usage else None}")

try:
    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "x"}])
except APIStatusError as e:
    body = e.body if isinstance(e.body, dict) else {}
    show("3 unknown model", f"HTTP {e.status_code} {body.get('code')}")

m = client.chat.completions.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "Email ravi@example.com the invoice"}],
    max_tokens=50,
)
show("4 personal data", repr(m.choices[0].message.content))
show("5 models", str([x.id for x in client.models.list()]))
show("6 status", httpx.get(f"{BASE}/v1/governance/status", headers=auth).json()["risk_tier"])

body2 = {
    "model": "claude-sonnet",
    "messages": [{"role": "user", "content": "once"}],
    "max_tokens": 20,
}
key = {"Idempotency-Key": f"try-{uuid4().hex}"}  # fresh key per run
r1 = httpx.post(f"{BASE}/v1/chat/completions", headers={**auth, **key}, json=body2)
r2 = httpx.post(f"{BASE}/v1/chat/completions", headers={**auth, **key}, json=body2)
show("7 idempotency", f"{r1.status_code} then {r2.status_code} {r2.json()['error']['code']}")
