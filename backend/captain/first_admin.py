"""Create the first administrator of an install and print a one-time sign-in link.

    python -m captain.first_admin EMAIL [--name NAME] [--platform-admin]

For a new install that has no SSO yet (QA, or before the org admin connects SSO). The
person gets org-admin (and platform-admin with --platform-admin, on the Stixis install);
the link works once, for 7 days, and only while SSO is off. Inside Docker:

    docker compose exec admin python -m captain.first_admin you@company.com --platform-admin
"""

from __future__ import annotations

import argparse
from dataclasses import replace

from captain.api.org import INVITE_TTL_S, InviteService, normalise_email
from captain.api.state import PgStateStore
from captain.config import build_from_env
from captain.core.identity.model import PrincipalRecord
from captain.core.models import PrincipalType
from captain.envfile import current_env


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("email")
    ap.add_argument("--name", default="")
    ap.add_argument("--platform-admin", action="store_true", help="Stixis install only")
    a = ap.parse_args()
    env = current_env()
    if env.get("CAPTAIN_ADMIN_OIDC"):
        print("SSO is configured on this install: add the person on Users and sign in with SSO.")
        return 1
    email = normalise_email(a.email)
    roles = {"org-admin"} | ({"platform-admin"} if a.platform_admin else set())
    built, settings = build_from_env()
    ids = built.infra.identity_store
    p = ids.get_principal(email)
    if p is None:
        name = a.name.strip() or email.split("@", 1)[0]
        p = PrincipalRecord(
            email, PrincipalType.USER.value, "default", name, roles=frozenset(roles)
        )
    else:
        p = replace(p, roles=p.roles | roles, active=True)
    ids.put_principal(p)
    from sqlalchemy import create_engine

    store = PgStateStore(create_engine(settings.database_url), ttl_s=INVITE_TTL_S)
    token, _ = InviteService(store, sso_enabled=lambda: False).create(email, "first-admin")
    base = (env.get("CAPTAIN_PUBLIC_CONSOLE_URL") or "").rstrip("/")
    if env.get("CAPTAIN_MULTI_TENANT") == "1":  # shared Captain: the Stixis console address
        host = env.get("CAPTAIN_PLATFORM_HOST") or f"captain.{env.get('CAPTAIN_BASE_DOMAIN', '')}"
        base = f"{env.get('CAPTAIN_PUBLIC_SCHEME', 'https')}://{host}"
    print(f"{email} is now {', '.join(sorted(p.roles))}.")
    print("One-time sign-in link (works once, for 7 days; do not share it):")
    print(f"{base}/accept-invite#token={token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
