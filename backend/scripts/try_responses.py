"""Try the Captain gateway's OpenAI Responses endpoint with the official OpenAI SDK.

Install a recent `openai` in a SEPARATE virtual environment (the Captain backend pins an
older one), then:

    python scripts/try_responses.py

Needs the services from the README running (OPA, worker, mock provider, gateway) and the
dev key from `python -m captain.devseed` in backend/.dev/dev-app.key. The mock provider
only produces text, so function calls are covered by the unit tests.
"""

from pathlib import Path

import openai
from openai import OpenAI

KEY = (Path(__file__).resolve().parents[1] / ".dev" / "dev-app.key").read_text().strip()
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key=KEY, max_retries=0)


def show(label: str, text: object) -> None:
    print(f"{label:<14}: {text}")


r = client.responses.create(
    model="claude-sonnet", instructions="Be brief.", input="Summarise our refund policy"
)
show("1 normal", f"{r.output_text!r} | {r.status} | tokens {r.usage.total_tokens}")

with client.responses.stream(model="claude-sonnet", input="stream this") as s:
    kinds = [e.type for e in s]
    final = s.get_final_response()
show("2 streaming", f"{final.output_text!r} | {final.status} | {len(kinds)} events")

try:
    client.responses.create(model="gpt-4o", input="x")
except openai.APIStatusError as e:
    show("3 unknown model", f"HTTP {e.status_code} {e.body['code']}")

r = client.responses.create(model="claude-sonnet", input="Email ravi@example.com the invoice")
show("4 personal data", repr(r.output_text))

try:
    client.responses.create(model="claude-sonnet", input="x", previous_response_id="resp_old")
except openai.BadRequestError as e:
    show("5 stateless", f"HTTP {e.status_code}")
