"""Try the Captain gateway's Anthropic Messages endpoint with the official Anthropic SDK.

Install the SDK in a SEPARATE virtual environment (`pip install anthropic`), then:

    python scripts/try_messages.py

Needs the services from the README running (OPA, worker, mock provider, gateway) and the
dev key from `python -m captain.devseed` in backend/.dev/dev-app.key. The mock provider
only produces text, so tool use is covered by the unit tests, not by this script.
"""

from pathlib import Path

import anthropic

KEY = (Path(__file__).resolve().parents[1] / ".dev" / "dev-app.key").read_text().strip()
client = anthropic.Anthropic(base_url="http://127.0.0.1:8000", api_key=KEY, max_retries=0)


def show(label: str, text: object) -> None:
    print(f"{label:<14}: {text}")


m = client.messages.create(
    model="claude-sonnet",
    max_tokens=100,
    system="Be brief.",
    messages=[{"role": "user", "content": "Summarise our refund policy"}],
)
u = m.usage
show("1 normal", f"{m.content[0].text!r} | {m.stop_reason} | {u.input_tokens}/{u.output_tokens}")

with client.messages.stream(
    model="claude-sonnet", max_tokens=100, messages=[{"role": "user", "content": "stream this"}]
) as s:
    text = "".join(s.text_stream)
    final = s.get_final_message()
show("2 streaming", f"{text!r} | {final.stop_reason} | tokens {final.usage.output_tokens}")

try:
    client.messages.create(
        model="gpt-4o", max_tokens=10, messages=[{"role": "user", "content": "x"}]
    )
except anthropic.APIStatusError as e:
    show("3 unknown model", f"HTTP {e.status_code} {e.body['error']['type']}")

m = client.messages.create(
    model="claude-sonnet",
    max_tokens=50,
    messages=[{"role": "user", "content": "Email ravi@example.com the invoice"}],
)
show("4 personal data", repr(m.content[0].text))
show(
    "5 count",
    client.messages.count_tokens(
        model="claude-sonnet", messages=[{"role": "user", "content": "hello"}]
    ).input_tokens,
)
