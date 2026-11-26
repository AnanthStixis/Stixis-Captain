from __future__ import annotations

from datetime import timedelta

import pytest

from captain.core.bus import InMemoryBus
from captain.core.decision_api import AuthenticationError
from captain.core.identity.keys import MalformedKeyError, generate_key, parse_key
from captain.core.identity.killswitch import InMemoryKillSwitchStore, KillScope, KillSwitch
from captain.core.identity.model import PrincipalRecord
from captain.core.identity.rbac import Permission, PermissionDenied, require
from captain.core.identity.service import IdentityService, InMemoryIdentityStore
from captain.core.models import Environment, ReasonCode

PEPPER = b"p" * 32


@pytest.fixture
def ids() -> IdentityService:
    store = InMemoryIdentityStore()
    store.put_principal(PrincipalRecord(id="u1", type="USER", org_id="o1", display_name="U"))
    return IdentityService(store, PEPPER, InMemoryBus())


def issue(ids: IdentityService, ttl: timedelta | None = None, cidrs: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    return ids.issue_key(
        "u1", "AI-1", "p1", "a1", Environment.DEVELOPMENT, ttl, allowed_cidrs=cidrs
    )


def test_key_round_trip_and_checksum() -> None:
    raw, parsed = generate_key()
    assert parse_key(raw) == parsed and raw.startswith("cpt_live_")
    tampered = raw[:-1] + ("0" if raw[-1] != "0" else "1")
    with pytest.raises(MalformedKeyError):
        parse_key(tampered)


def test_raw_key_never_stored(ids: IdentityService) -> None:
    k = issue(ids)
    assert k.raw_key not in repr(k.record) and parse_key(k.raw_key).secret not in repr(k.record)
    assert k.raw_key not in repr(k)


def test_authenticate_and_last_used(ids: IdentityService) -> None:
    k = issue(ids)
    rec = ids.authenticate(k.raw_key)
    assert rec.key_id == k.record.key_id


def test_wrong_secret_same_keyid_rejected(ids: IdentityService) -> None:
    k = issue(ids)
    raw2, _ = generate_key()
    forged_body = "_".join(k.raw_key.split("_")[:3] + [raw2.split("_")[3]])
    import zlib

    forged = f"{forged_body}_{zlib.crc32(forged_body.encode()) & 0xFFFFFFFF:08x}"
    with pytest.raises(AuthenticationError):
        ids.authenticate(forged)


def test_revocation_is_immediate_despite_cache(ids: IdentityService) -> None:
    k = issue(ids)
    ids.authenticate(k.raw_key)  # warm cache
    ids.revoke_key(k.record.key_id, "test")
    with pytest.raises(AuthenticationError) as e:
        ids.authenticate(k.raw_key)
    assert e.value.reason_code is ReasonCode.KEY_REVOKED


def test_expired_key(ids: IdentityService) -> None:
    k = issue(ids, ttl=timedelta(seconds=-1))
    with pytest.raises(AuthenticationError):
        ids.authenticate(k.raw_key)


def test_rotation_revokes_old(ids: IdentityService) -> None:
    k = issue(ids)
    new = ids.rotate_key(k.record.key_id)
    assert new.record.rotated_from == k.record.key_id
    ids.authenticate(new.raw_key)
    with pytest.raises(AuthenticationError):
        ids.authenticate(k.raw_key)


def test_cidr_allowlist(ids: IdentityService) -> None:
    k = issue(ids, cidrs=("10.0.0.0/8",))
    ids.authenticate(k.raw_key, "10.1.2.3")
    with pytest.raises(AuthenticationError):
        ids.authenticate(k.raw_key, "192.168.1.1")


def test_rbac() -> None:
    require(frozenset({"auditor"}), Permission.AUDIT_EXPORT)
    with pytest.raises(PermissionDenied):
        require(frozenset({"auditor"}), Permission.KILL_SWITCH)
    with pytest.raises(PermissionDenied):  # admin does not see content by default
        require(frozenset({"admin"}), Permission.CONTENT_READ)
    with pytest.raises(PermissionDenied):
        require(frozenset({"made-up-role"}), Permission.USAGE_READ)


def test_kill_switch_match() -> None:
    ks = KillSwitch(InMemoryKillSwitchStore())
    ks.activate(KillScope.MODEL, "gpt-x", "incident", "admin-1")
    assert ks.match({KillScope.MODEL: "gpt-x", KillScope.PRINCIPAL: "u1"}) is not None
    assert ks.match({KillScope.MODEL: "claude"}) is None
    ks.deactivate(KillScope.MODEL, "gpt-x")
    assert ks.match({KillScope.MODEL: "gpt-x"}) is None
