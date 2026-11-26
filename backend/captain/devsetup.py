"""Local development setup:  python -m captain.devsetup [--sql-out PATH]

Creates, if missing (never overwrites existing values):
- backend/.env entries: CAPTAIN_DATABASE_URL (dev database), CAPTAIN_KEY_PEPPER,
  CAPTAIN_LEDGER_KEY, CAPTAIN_WORM_DIR, CAPTAIN_OPA_URL, CAPTAIN_APPROVERS
- backend/.dev/ledger.pem (Ed25519 ledger signing key) and backend/.dev/anchors/
- a SQL file that creates the `captain_dev` role and database (run it with psql as a
  superuser; it contains the generated dev password, so delete it afterwards)

Secrets are written only to gitignored files and are never printed.
Development only: production secrets come from the client's secret store.
"""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path

from captain.core.ledger.signers import PemFileSigner
from captain.envfile import DEFAULT_ENV_FILE, dotenv_values

BACKEND = DEFAULT_ENV_FILE.parent
DEV = BACKEND / ".dev"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sql-out", type=Path, default=DEV / "create_captain_dev.sql")
    a = ap.parse_args()

    env = dotenv_values(DEFAULT_ENV_FILE)
    add: dict[str, str] = {}
    db_password = None
    if "CAPTAIN_DATABASE_URL" not in env:
        db_password = secrets.token_hex(24)
        add["CAPTAIN_DATABASE_URL"] = (
            f"postgresql+psycopg://captain_dev:{db_password}@localhost:5432/captain_dev"
        )
    if "CAPTAIN_KEY_PEPPER" not in env:
        add["CAPTAIN_KEY_PEPPER"] = secrets.token_hex(32)
    key = DEV / "ledger.pem"
    if not key.exists():
        PemFileSigner.generate(key)
    env_defaults = {
        "CAPTAIN_LEDGER_KEY": f"pem:{key}",
        "CAPTAIN_LEDGER_KEY_ID": "dev-ledger-1",
        "CAPTAIN_WORM_DIR": str(DEV / "anchors"),
        "CAPTAIN_OPA_URL": "http://127.0.0.1:8181",
        "CAPTAIN_APPROVERS": '{"*": ["approver-1"]}',
        "CAPTAIN_ESCALATION_APPROVERS": "approver-2",
        "CAPTAIN_MOCK_PROVIDER_KEY": "dev-mock-key",
        "CAPTAIN_MOCK_MCP_KEY": "mock-mcp-key",  # mock MCP server only
        "CAPTAIN_MCP_ALLOW_LOOPBACK": "1",  # dev only: lets the proxy call http://127.0.0.1
        "CAPTAIN_DEV_ADMIN_TOKEN": secrets.token_urlsafe(
            32
        ),  # dashboard sign-in, this machine only  # mock provider only checks one is sent
    }
    # Local single sign-on against the mock identity provider (python -m captain.devidp).
    idp_secret = env.get("CAPTAIN_DEV_IDP_CLIENT_SECRET") or secrets.token_urlsafe(32)
    env_defaults["CAPTAIN_DEV_IDP_CLIENT_SECRET"] = idp_secret
    env_defaults["CAPTAIN_SESSION_SECRET"] = secrets.token_hex(32)
    env_defaults["CAPTAIN_ADMIN_OIDC"] = json.dumps(
        {
            "issuer": "http://127.0.0.1:8200",
            "client_id": "captain-console",
            "client_secret": idp_secret,
            "redirect_uri": "http://127.0.0.1:8100/admin/v1/auth/callback",
            "group_roles": {
                "grp-captain-admins": ["admin", "approver", "auditor"],
                "grp-approvers": ["approver", "viewer"],
                "grp-finops": ["finops", "viewer"],
                "grp-auditors": ["auditor", "viewer"],
            },
        },
        separators=(",", ":"),
    )
    for k, v in env_defaults.items():
        if k not in env:
            add[k] = v

    if add:
        existing = DEFAULT_ENV_FILE.read_text(encoding="utf-8") if DEFAULT_ENV_FILE.exists() else ""
        block = "\n# --- local development (python -m captain.devsetup) ---\n"
        block += "".join(f"{k}={v}\n" for k, v in add.items())
        DEFAULT_ENV_FILE.write_text(existing.rstrip("\n") + "\n" + block, encoding="utf-8")
    print(f"backend/.env: added {sorted(add) or 'nothing (already configured)'}")

    if db_password:
        a.sql_out.parent.mkdir(parents=True, exist_ok=True)
        a.sql_out.write_text(
            "\\set ON_ERROR_STOP on\n"  # noqa: S608 (generated hex password)
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='captain_dev') "
            f"THEN CREATE ROLE captain_dev LOGIN PASSWORD '{db_password}'; "
            f"ELSE ALTER ROLE captain_dev LOGIN PASSWORD '{db_password}'; END IF; END $$;\n"
            "SELECT 'CREATE DATABASE captain_dev OWNER captain_dev' "
            "WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='captain_dev')\\gexec\n"
            "\\echo captain_dev role and database are ready\n",
            encoding="utf-8",
        )
        print(f"database SQL written to {a.sql_out} (run with psql as postgres, then delete it)")


if __name__ == "__main__":
    main()
