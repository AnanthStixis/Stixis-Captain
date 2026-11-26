"""Company profile, users and invites on PostgreSQL."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from captain.api.org import (
    INVITE_TTL_S,
    Contact,
    InviteError,
    InviteService,
    Organization,
    PgOrganizationStore,
)
from captain.api.state import PgStateStore
from captain.core.identity.model import PrincipalRecord
from captain.db.stores import PgIdentityStore


def test_organization_row_round_trip(pg: Engine) -> None:
    s = PgOrganizationStore(pg)
    org, at, by = s.get()
    assert org == Organization() and at is None and by is None
    want = Organization(
        company_name="Acme",
        legal_name="Acme Ltd",
        domains=["acme.example"],
        country="IN",
        industry="Finance",
        contacts=[Contact(role="security", name="Sam", email="sam@acme.example")],
        adopters=["claude-code"],
        checklist={"profile": True},
    )
    s.put(want, "boss@acme.example")
    s.put(want, "boss@acme.example")  # upsert, still one row
    got, at, by = s.get()
    assert got == want and at is not None and by == "boss@acme.example"
    with pg.connect() as c:
        assert c.execute(text("SELECT count(*) FROM organization")).scalar() == 1


def test_user_records_persist_roles_and_active(pg: Engine) -> None:
    ids = PgIdentityStore(pg)
    ids.put_principal(
        PrincipalRecord("p@acme.example", "USER", "org", "Priya", "data", None,
                        frozenset({"finops", "org-admin"}))
    )  # fmt: skip
    p = ids.get_principal("p@acme.example")
    assert p is not None and p.roles == {"finops", "org-admin"} and p.active


def test_invite_single_use_across_replicas(pg: Engine) -> None:
    a = InviteService(PgStateStore(pg, ttl_s=INVITE_TTL_S), lambda: False)
    b = InviteService(PgStateStore(pg, ttl_s=INVITE_TTL_S), lambda: False)
    token, _ = a.create("first@acme.example", "platform")
    actor, sid, _ = b.redeem(token)
    assert actor.id == "first@acme.example"
    assert a.session(sid) is not None  # sessions are shared by every replica
    with pytest.raises(InviteError) as e:
        a.redeem(token)
    assert e.value.code == "invalid"
