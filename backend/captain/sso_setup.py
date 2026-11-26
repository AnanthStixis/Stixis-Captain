"""Set up single sign-on with a real identity provider:  python -m captain.sso_setup

Run it in YOUR terminal: it asks for the provider details, reads the client secret without
echoing it, writes CAPTAIN_ADMIN_OIDC (and a session secret if there is none) into
backend/.env (gitignored), then runs the SSO check. The secret is never printed and never
needs to be pasted anywhere else. The value being replaced is kept as
CAPTAIN_ADMIN_OIDC_PREVIOUS so the local mock provider setup can be restored.

Works for Okta, Microsoft Entra ID and any OpenID Connect provider; Okta is the default.
"""

from __future__ import annotations

import getpass
import json
import secrets
import sys
from pathlib import Path

from captain.core.identity.rbac import ROLE_PERMISSIONS
from captain.envfile import DEFAULT_ENV_FILE
from captain.sso_check import main as run_check

DEFAULT_REDIRECT = "http://localhost:8100/admin/v1/auth/callback"


def issuer_for(provider_host: str) -> str:
    """`dev-123.okta.com` -> https://dev-123.okta.com; a full URL (custom authorization
    server, Entra tenant) is kept as given, without a trailing slash."""
    v = provider_host.strip().rstrip("/")
    return v if v.startswith("https://") else f"https://{v}"


def parse_group_roles(lines: list[str]) -> dict[str, list[str]]:
    """'Captain Admins = admin, approver' per line -> {group: [roles]}."""
    out: dict[str, list[str]] = {}
    for line in lines:
        group, sep, roles = line.partition("=")
        names = [r.strip() for r in roles.split(",") if r.strip()]
        if not sep or not group.strip() or not names:
            raise ValueError(f"expected 'group = role, role' but got: {line!r}")
        unknown = [r for r in names if r not in ROLE_PERMISSIONS]
        if unknown:
            raise ValueError(f"unknown role(s) {unknown}; choose from {sorted(ROLE_PERMISSIONS)}")
        out[group.strip()] = names
    return out


def build_config(
    issuer: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    group_roles: dict[str, list[str]],
    scopes: str = "openid profile email groups",
) -> dict[str, object]:
    return {
        "issuer": issuer,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "scopes": scopes,
        "roles_claim": "groups",
        "name_claim": "preferred_username",
        "group_roles": group_roles,
    }


def update_env(path: Path, updates: dict[str, str], keep_previous: tuple[str, ...] = ()) -> None:
    """Set KEY=value lines in a .env file, keeping every other line. A replaced value listed
    in `keep_previous` is saved as KEY_PREVIOUS (one level, overwritten each time)."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    index = {ln.partition("=")[0].strip(): i for i, ln in enumerate(lines) if "=" in ln}
    extra: list[str] = []
    for key, value in updates.items():
        if key in keep_previous and key in index:
            old = lines[index[key]].partition("=")[2]
            prev = f"{key}_PREVIOUS={old}"
            if f"{key}_PREVIOUS" in index:
                lines[index[f"{key}_PREVIOUS"]] = prev
            else:
                extra.append(prev)
        new = f"{key}={value}"
        if key in index:
            lines[index[key]] = new
        else:
            extra.append(new)
    path.write_text("\n".join([*lines, *extra]) + "\n", encoding="utf-8")


def main() -> int:
    print("Captain single sign-on setup (Okta by default). Nothing is sent anywhere.\n")
    host = input("Okta domain or issuer URL (e.g. dev-123456.okta.com): ").strip()
    client_id = input("Client ID: ").strip()
    secret = getpass.getpass("Client secret (hidden): ").strip()
    redirect = input(f"Redirect URI [{DEFAULT_REDIRECT}]: ").strip() or DEFAULT_REDIRECT
    print("\nWhich provider groups get which Captain role? One per line, blank to finish.")
    print("  e.g.  Captain Admins = admin, approver, auditor")
    print(f"  roles: {', '.join(sorted(ROLE_PERMISSIONS))}")
    lines: list[str] = []
    while line := input("  group = roles: ").strip():
        lines.append(line)
    try:
        cfg = build_config(issuer_for(host), client_id, secret, redirect, parse_group_roles(lines))
    except ValueError as e:
        print(f"\n{e}")
        return 2
    updates = {"CAPTAIN_ADMIN_OIDC": json.dumps(cfg, separators=(",", ":"))}
    if "CAPTAIN_SESSION_SECRET=" not in DEFAULT_ENV_FILE.read_text(encoding="utf-8"):
        updates["CAPTAIN_SESSION_SECRET"] = secrets.token_hex(32)
    update_env(DEFAULT_ENV_FILE, updates, keep_previous=("CAPTAIN_ADMIN_OIDC",))
    print(f"\nWritten to {DEFAULT_ENV_FILE} (the secret is not shown). Checking it:\n")
    return run_check(["--url"])


if __name__ == "__main__":
    sys.exit(main())
