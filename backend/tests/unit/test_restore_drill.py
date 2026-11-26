"""The restore drill's comparison and safety helpers (the drill itself needs PostgreSQL tools)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from restore_drill import compare, libpq  # noqa: E402


def test_identical_ledgers_compare_clean_and_differences_are_named() -> None:
    a = ({"AI-DEV": 10, "admin": 3}, {"AI-DEV": "h1", "admin": "h2"})
    assert compare(a, a) == []
    short = ({"AI-DEV": 9, "admin": 3}, {"AI-DEV": "h0", "admin": "h2"})
    assert compare(a, short) == ["partition AI-DEV: 10 in source, 9 restored"]
    forked = ({"AI-DEV": 10, "admin": 3}, {"AI-DEV": "h1", "admin": "XX"})
    assert compare(a, forked) == ["partition admin: newest entry hash differs"]
    missing = ({"AI-DEV": 10}, {"AI-DEV": "h1"})
    assert "partition admin: 3 in source, None restored" in compare(a, missing)


def test_password_goes_in_the_environment_not_the_command_line() -> None:
    args, env = libpq("postgresql+psycopg://u:s3cret@db.example:5433/captain")
    assert "s3cret" not in " ".join(args) and env["PGPASSWORD"] == "s3cret"
    assert args[-2:] == ["-d", "captain"] and "-p" in args and "5433" in args
    with pytest.raises(SystemExit):
        libpq("postgresql+psycopg://u:p@h")
