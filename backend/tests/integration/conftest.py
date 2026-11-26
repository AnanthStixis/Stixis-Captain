"""Integration fixtures for a local PostgreSQL test database.

Set CAPTAIN_TEST_DATABASE_URL, e.g.
    postgresql+psycopg://captain_test@localhost:5432/captain_test
The schema is migrated up before the session and fully dropped afterwards, so use a
database dedicated to tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

BACKEND = Path(__file__).resolve().parents[2]


def _from_dotenv(name: str) -> str | None:
    """Read NAME=value from backend/.env (gitignored) so secrets stay out of the shell."""
    env = BACKEND / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k.strip() == name:
            return v.strip().strip('"')
    return None


URL = os.environ.get("CAPTAIN_TEST_DATABASE_URL") or _from_dotenv("CAPTAIN_TEST_DATABASE_URL")

pytestmark = pytest.mark.integration


def _alembic(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    os.environ["CAPTAIN_DATABASE_URL"] = url
    return cfg


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    if not URL:
        pytest.skip("CAPTAIN_TEST_DATABASE_URL not set")
    cfg = _alembic(URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(URL, pool_size=20, max_overflow=20)
    yield engine
    engine.dispose()
    command.downgrade(cfg, "base")


@pytest.fixture
def pg(pg_engine: Engine) -> Engine:
    """Clean data between tests. The ledger trigger blocks TRUNCATE, so it is disabled
    here by the owning (break-glass) role — exactly the path the hash chain and anchors
    exist to catch."""
    with pg_engine.begin() as c:
        c.execute(text("ALTER TABLE evidence_ledger DISABLE TRIGGER USER"))
        c.execute(text(
            "TRUNCATE evidence_ledger, ledger_outbox, ledger_anchor_ranges, approval_tickets, "
            "budget_reservations, budget_buckets, rate_windows, budget_definitions, rate_limits, "
            "kill_switches, workload_bindings, virtual_keys, principals, applications, projects, "
            "register_entries, organization, provider_secrets, cp_companies, "
            "model_prices, alerts, registry_items, budget_alert_marks, api_state, subject_keys"
        ))  # fmt: skip
        c.execute(text("ALTER TABLE evidence_ledger ENABLE TRIGGER USER"))
    return pg_engine
