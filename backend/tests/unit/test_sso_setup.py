"""The SSO setup helper: building the settings and editing .env safely."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from captain.sso_check import FAIL, check_config
from captain.sso_setup import build_config, issuer_for, parse_group_roles, update_env


def test_issuer_from_an_okta_domain_or_a_full_url() -> None:
    assert issuer_for("dev-123456.okta.com") == "https://dev-123456.okta.com"
    assert (
        issuer_for(" https://dev-1.okta.com/oauth2/default/ ")
        == "https://dev-1.okta.com/oauth2/default"
    )
    assert (
        issuer_for("https://login.microsoftonline.com/t/v2.0")
        == "https://login.microsoftonline.com/t/v2.0"
    )


def test_group_roles_are_parsed_and_checked_against_real_role_names() -> None:
    got = parse_group_roles(["Captain Admins = admin, approver", "Viewers=viewer"])
    assert got == {"Captain Admins": ["admin", "approver"], "Viewers": ["viewer"]}
    for bad in ["no equals sign", "= admin", "Group =", "Group = adminn"]:
        with pytest.raises(ValueError):
            parse_group_roles([bad])


def test_update_env_keeps_other_lines_and_saves_the_previous_value(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text('A=1\n# comment\nCAPTAIN_ADMIN_OIDC={"old":true}\nB=2\n', encoding="utf-8")
    update_env(
        f, {"CAPTAIN_ADMIN_OIDC": '{"new":true}', "C": "3"}, keep_previous=("CAPTAIN_ADMIN_OIDC",)
    )
    text = f.read_text(encoding="utf-8")
    assert "A=1" in text and "# comment" in text and "B=2" in text and "C=3" in text
    assert 'CAPTAIN_ADMIN_OIDC={"new":true}' in text
    assert 'CAPTAIN_ADMIN_OIDC_PREVIOUS={"old":true}' in text
    update_env(f, {"CAPTAIN_ADMIN_OIDC": '{"newer":true}'}, keep_previous=("CAPTAIN_ADMIN_OIDC",))
    again = f.read_text(encoding="utf-8")
    assert again.count("CAPTAIN_ADMIN_OIDC_PREVIOUS=") == 1  # one level, not a growing pile
    assert 'CAPTAIN_ADMIN_OIDC_PREVIOUS={"new":true}' in again
    update_env(tmp_path / "fresh.env", {"X": "1"})  # a missing file is created
    assert (tmp_path / "fresh.env").read_text(encoding="utf-8") == "X=1\n"


def test_the_written_settings_pass_the_configuration_check() -> None:
    cfg = build_config(
        issuer_for("dev-123456.okta.com"),
        "0oa1abcdEXAMPLE",
        "s" * 40,
        "http://localhost:8100/admin/v1/auth/callback",
        parse_group_roles(["Captain Admins = admin, approver, auditor"]),
    )
    env = {"CAPTAIN_ADMIN_OIDC": json.dumps(cfg), "CAPTAIN_SESSION_SECRET": "ab" * 32}
    parsed, findings = check_config(env)
    assert parsed is not None and not any(f.level == FAIL for f in findings)
    assert cfg["scopes"] == "openid profile email groups"  # Okta needs the groups scope
