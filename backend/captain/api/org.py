"""Company profile, users and the first org-admin sign-in for one Captain install.

- `organization`: one row per install (company name, domains, contacts, chosen adopters,
  onboarding checklist). Each company keeps its own isolated install, so there is no
  tenant column to filter on; the row id is fixed.
- Users are principals of type USER whose id is their email address. Their console roles
  are added to whatever the identity provider grants (see `actor_from_claims`).
- Org-admin invite: before SSO is connected nobody can sign in, so the platform (or an
  admin with the dev token) creates a one-time link. Only the SHA-256 of the token is
  stored; it expires after 7 days, can be redeemed once, and is refused (as are the
  sessions it opened) as soon as SSO is configured.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from captain.api.admin_sso import SAFE_METHODS, CsrfError
from captain.api.state import StateStore
from captain.core.admin import AdminActor
from captain.core.decision_api import AuthenticationError

ORG_ROW = "install"
INVITE_KIND = "org_invite"
INVITE_SESSION_KIND = "invite_session"  # never shared with SSO sessions
INVITE_TTL_S = 7 * 24 * 3600
INVITE_SESSION_S = 8 * 3600
INVITE_COOKIE = "captain_invite_session"

CHECKLIST_ITEMS: tuple[tuple[str, str], ...] = (
    ("sso", "Connect your sign-in (SSO)"),
    ("profile", "Complete the company profile"),
    ("users", "Add the people who will use the console"),
    ("adopters", "Choose how teams will adopt Captain"),
)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


def normalise_email(v: str) -> str:
    v = v.strip().lower()
    if not _EMAIL.match(v) or len(v) > 128:
        raise ValueError("a valid email address is required")
    return v


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Contact(_Model):
    role: str = Field(min_length=1, max_length=64)  # e.g. "security", "billing", "dpo"
    name: str = Field(min_length=1, max_length=200)
    email: str

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return normalise_email(v)


class Organization(_Model):
    company_name: str = Field(default="", max_length=200)
    legal_name: str = Field(default="", max_length=300)
    domains: list[str] = Field(default_factory=list, max_length=50)
    country: str = Field(default="", max_length=64)
    industry: str = Field(default="", max_length=120)
    contacts: list[Contact] = Field(default_factory=list, max_length=20)
    adopters: list[str] = Field(default_factory=list, max_length=50)
    checklist: dict[str, bool] = Field(default_factory=dict)

    @field_validator("domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for d in v:
            d = d.strip().lower()
            if not _DOMAIN.match(d):
                raise ValueError(f"not a domain name: {d[:80]!r}")
            if d not in out:
                out.append(d)
        return out

    @field_validator("adopters")
    @classmethod
    def _adopters(cls, v: list[str]) -> list[str]:
        out = []
        for a in v:
            a = a.strip()
            if not a or len(a) > 64:
                raise ValueError("adopter names must be 1-64 characters")
            if a not in out:
                out.append(a)
        return out

    @field_validator("checklist")
    @classmethod
    def _checklist(cls, v: dict[str, bool]) -> dict[str, bool]:
        known = {k for k, _ in CHECKLIST_ITEMS}
        unknown = set(v) - known
        if unknown:
            raise ValueError(f"unknown checklist item(s): {sorted(unknown)}")
        return v


class OrganizationStore(Protocol):
    def get(self) -> tuple[Organization, datetime | None, str | None]: ...
    def put(self, org: Organization, actor_id: str) -> None: ...


class InMemoryOrganizationStore:
    def __init__(self) -> None:
        self._row: tuple[Organization, datetime | None, str | None] = (Organization(), None, None)
        self._lock = threading.Lock()

    def get(self) -> tuple[Organization, datetime | None, str | None]:
        return self._row

    def put(self, org: Organization, actor_id: str) -> None:
        with self._lock:
            self._row = (org.model_copy(deep=True), datetime.now(UTC), actor_id)


class PgOrganizationStore:
    def __init__(self, engine: Any) -> None:
        self._e = engine

    def get(self) -> tuple[Organization, datetime | None, str | None]:
        from sqlalchemy import select

        from captain.db.schema import organization as t

        with self._e.connect() as c:
            r = c.execute(select(t).where(t.c.id == ORG_ROW)).first()
        if r is None:
            return Organization(), None, None
        org = Organization(
            company_name=r.company_name,
            legal_name=r.legal_name,
            domains=list(r.domains or ()),
            country=r.country,
            industry=r.industry,
            contacts=[Contact(**c) for c in (r.contacts or [])],
            adopters=list(r.adopters or ()),
            checklist=dict(r.checklist or {}),
        )
        return org, r.updated_at, r.updated_by

    def put(self, org: Organization, actor_id: str) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from captain.db.schema import organization as t

        row = {
            "id": ORG_ROW,
            "company_name": org.company_name,
            "legal_name": org.legal_name,
            "domains": org.domains,
            "country": org.country,
            "industry": org.industry,
            "contacts": [c.model_dump() for c in org.contacts],
            "adopters": org.adopters,
            "checklist": org.checklist,
            "updated_at": datetime.now(UTC),
            "updated_by": actor_id,
        }
        stmt = pg_insert(t).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"], set_={k: stmt.excluded[k] for k in row if k != "id"}
        )
        with self._e.begin() as c:
            c.execute(stmt)


# ----------------------------------------------------------------- invites
class InviteError(AuthenticationError):
    """Safe-to-show reason an invite cannot be used: invalid | sso_on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class InviteService:
    """One-time org-admin invite links and the console sessions they open.

    `sso_enabled` is asked on every use, so turning SSO on immediately ends invite
    redemption and every session an invite opened."""

    def __init__(
        self,
        store: StateStore,
        sso_enabled: Callable[[], bool],
        now: Callable[[], float] = time.time,
        secure_cookies: bool = False,
    ) -> None:
        self._store = store
        self._sso = sso_enabled
        self._now = now
        self.cookie_name = ("__Host-" if secure_cookies else "") + INVITE_COOKIE
        self.secure_cookies = secure_cookies

    def create(self, email: str, created_by: str) -> tuple[str, float]:
        """Returns (raw token, expiry). The raw token is shown once and never stored."""
        if self._sso():
            raise InviteError("sso_on", "SSO is configured; add the person as a user instead")
        token = secrets.token_urlsafe(32)
        exp = self._now() + INVITE_TTL_S
        self._store.put(
            INVITE_KIND,
            _hash(token),
            "invite",
            {"email": normalise_email(email), "by": created_by, "exp": exp},
        )
        return token, exp

    def redeem(self, token: str) -> tuple[AdminActor, str, str]:
        """Single use: the record is deleted atomically; only the caller that deleted it wins.
        Returns (actor, session id, csrf token)."""
        if self._sso():
            raise InviteError("sso_on", "SSO is configured; sign in with SSO instead")
        if not token or len(token) > 200:
            raise InviteError("invalid", "this invite link is not valid")
        key = _hash(token)
        doc = self._store.get(INVITE_KIND, key, "invite")
        if doc is None or not self._store.delete(INVITE_KIND, key):
            raise InviteError("invalid", "this invite link is not valid or was already used")
        if float(doc["exp"]) <= self._now():
            raise InviteError("invalid", "this invite link has expired")
        actor = AdminActor(str(doc["email"]), frozenset({"org-admin"}))
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._store.put(
            INVITE_SESSION_KIND,
            _hash(sid),
            "session",
            {"id": actor.id, "csrf": csrf, "exp": self._now() + INVITE_SESSION_S},
        )
        return actor, sid, csrf

    def session(self, session_id: str | None) -> tuple[AdminActor, str] | None:
        if not session_id or self._sso():
            return None
        d = self._store.get(INVITE_SESSION_KIND, _hash(session_id), "session")
        if d is None or float(d["exp"]) <= self._now():
            return None
        return AdminActor(str(d["id"]), frozenset({"org-admin"})), str(d["csrf"])

    def end(self, session_id: str) -> None:
        self._store.delete(INVITE_SESSION_KIND, _hash(session_id))

    @staticmethod
    def check_csrf(method: str, headers: Any, expected: str) -> None:
        if method in SAFE_METHODS:
            return
        if not hmac.compare_digest(headers.get("x-csrf-token", ""), expected):
            raise CsrfError("missing or invalid CSRF token")
