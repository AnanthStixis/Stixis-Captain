"""A test PERSON for the erasure and live-evaluation tests (development only).

    .venv\\Scripts\\python scripts\\dev_person.py setup        create the person and their key
    .venv\\Scripts\\python scripts\\dev_person.py chat         send 3 chats as the person
    .venv\\Scripts\\python scripts\\dev_person.py chat --n 1 --text "LOWSCORE please"
    .venv\\Scripts\\python scripts\\dev_person.py status       what exists now

Creates, through the audited admin path:
- principal `test.person@corp.example` (type USER, so Captain treats it as personal data)
- AI Register entry `AI-EVAL` (Tier 2, A2, model `claude-sonnet` = the free mock model),
  kept separate from AI-DEV so the C1-C8 test state is not touched
- an application and a key, saved to backend/.dev/test-person.key (never printed)

After the person is erased, `setup` creates them again with a fresh key (a new person, as
far as Captain is concerned). Chats use the mock model: no real spend.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from captain.config import build_from_env  # noqa: E402
from captain.core.admin import AdminActor, AdminService  # noqa: E402
from captain.core.context.service import Application  # noqa: E402
from captain.core.identity.model import PrincipalRecord  # noqa: E402
from captain.core.models import AutonomyLevel, Environment, RegisterEntry, RiskTier  # noqa: E402
from captain.devsetup import DEV  # noqa: E402

PERSON = "test.person@corp.example"
REGISTER = "AI-EVAL"
APP = "eval-application"
KEY_FILE = DEV / "test-person.key"
GATEWAY = "http://127.0.0.1:8000"
ACTOR = AdminActor("dev-person-script", frozenset({"admin"}))


def setup() -> None:
    built, _ = build_from_env()
    infra = built.infra
    admin = AdminService(built.core, built.register, infra.budget_catalog)  # type: ignore[arg-type]
    fresh = infra.identity_store.get_principal(PERSON) is None
    if fresh:
        infra.identity_store.put_principal(
            PrincipalRecord(id=PERSON, type="USER", org_id="dev-org",
                            display_name="Test Person", team_id="dev-team")
        )  # fmt: skip
    if built.register.get(REGISTER) is None:
        admin.upsert_register(
            ACTOR,
            RegisterEntry(
                id=REGISTER, name="Evaluation test system", risk_tier=RiskTier.TIER_2,
                autonomy=AutonomyLevel.A2, owner_id="dev-owner", cost_centre="CC-DEV",
                purpose="Local erasure and live-evaluation tests",
                allowed_models=frozenset({"claude-sonnet"}),
            ),
        )  # fmt: skip
    infra.context_store.put_application(
        Application(APP, "dev-project", REGISTER, "Evaluation test application")
    )
    if fresh or not KEY_FILE.exists():
        issued = admin.issue_key(ACTOR, PERSON, REGISTER, "dev-project", APP,
                                 Environment.DEVELOPMENT, None, "test person")  # fmt: skip
        KEY_FILE.write_text(issued.raw_key, encoding="utf-8")
        print(f"created {PERSON} with a new key (saved to {KEY_FILE.name})")
    else:
        print(f"{PERSON} already exists; key file kept")
    print(f"system {REGISTER}: Tier 2, model claude-sonnet (mock, no spend)")


def chat(n: int, text: str) -> None:
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    for i in range(1, n + 1):
        r = httpx.post(
            f"{GATEWAY}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "claude-sonnet", "max_tokens": 50,
                  "messages": [{"role": "user", "content": f"{text} ({i})"}]},
            timeout=30,
        )  # fmt: skip
        if r.status_code == 200:
            reply = r.json()["choices"][0]["message"]["content"]
            print(f"{i}: 200 {r.headers.get('x-captain-decision')}  {reply[:70]}")
        else:
            err = r.json().get("error", r.json())
            print(f"{i}: {r.status_code} {err.get('code', '')} {err.get('message', '')[:90]}")


def status() -> None:
    built, _ = build_from_env()
    p = built.infra.identity_store.get_principal(PERSON)
    keys = built.infra.identity_store.keys_for_principal(PERSON)
    print(f"principal {PERSON}: {'exists' if p else 'none (never created, or erased)'}")
    for k in keys:
        print(f"  key {k.key_id}: {k.status.value}")
    entry = built.register.get(REGISTER)
    print(f"{REGISTER}: {entry.risk_tier.value if entry else 'missing'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    c = sub.add_parser("chat")
    c.add_argument("--n", type=int, default=3)
    c.add_argument("--text", default="Hello from the test person")
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "setup":
        setup()
    elif a.cmd == "chat":
        chat(a.n, a.text)
    else:
        status()


if __name__ == "__main__":
    main()
