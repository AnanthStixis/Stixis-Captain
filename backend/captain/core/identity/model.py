"""Identity records (mutable configuration, persisted by an IdentityStore)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

from captain.core.models import Environment


class KeyStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


@dataclass(frozen=True, slots=True)
class KeyRecord:
    key_id: str
    secret_hash: str
    principal_id: str
    register_id: str
    project_id: str
    application_id: str
    environment: Environment
    created_at: datetime
    expires_at: datetime | None = None
    status: KeyStatus = KeyStatus.ACTIVE
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    rotated_from: str | None = None
    last_used_at: datetime | None = None
    allowed_cidrs: tuple[str, ...] = ()
    label: str = ""

    def is_usable(self, now: datetime) -> bool:
        if self.status is not KeyStatus.ACTIVE:
            return False
        return self.expires_at is None or now < self.expires_at

    def ip_allowed(self, ip: str | None) -> bool:
        if not self.allowed_cidrs:
            return True
        if ip is None:
            return False
        addr = ip_address(ip)
        nets: list[IPv4Network | IPv6Network] = [ip_network(c) for c in self.allowed_cidrs]
        return any(addr in n for n in nets)

    def revoked(self, reason: str) -> KeyRecord:
        return replace(
            self, status=KeyStatus.REVOKED, revoked_at=datetime.now(UTC), revoked_reason=reason
        )


@dataclass(frozen=True, slots=True)
class PrincipalRecord:
    id: str
    type: str  # PrincipalType value
    org_id: str
    display_name: str
    team_id: str | None = None
    idp_subject: str | None = None
    roles: frozenset[str] = field(default_factory=frozenset)
    active: bool = True
