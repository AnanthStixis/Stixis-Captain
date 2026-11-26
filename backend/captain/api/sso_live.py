"""SSO set up by the org admin on the Setup page (no settings files).

Flow: save the provider details (a draft) -> "Test sign-in" (a real SSO round trip that
opens no session) -> "Turn on". Only a draft that passed its test can be turned on, so an
org admin cannot lock the company out with a typo. Turning SSO on ends invite and email-link
sessions (they are refused while SSO is on).

Everything, including the client secret and the session-cookie key, is stored as ONE
encrypted blob in the company's secret store (AES-256-GCM, data key wrapped by Key Vault):
provider id SSO_SECRET_ID. Installs that set CAPTAIN_ADMIN_OIDC keep using it and the
screen shows the settings as managed by the install (read-only)."""

import json
import secrets as pysecrets
import threading
from datetime import UTC, datetime
from typing import Any

from captain.api.admin_auth import AdminSsoConfig, OidcAdminAuthenticator
from captain.api.admin_sso import SsoClientConfig, SsoService
from captain.api.state import StateStore
from captain.core.admin import AdminActor
from captain.core.decision_api import AuthenticationError
from captain.core.identity.oidc import HttpJwks, IssuerConfig
from captain.core.secrets_store import SecretsService

SSO_SECRET_ID = "captain-sso"  # noqa: S105 - provider id of the encrypted settings blob
CALLBACK_PATH = "/admin/v1/auth/callback"


class SsoSetupError(ValueError):
    pass


class _DiscoveredJwks:
    """JWKS location from the IdP's discovery document, fetched on first use."""

    def __init__(self, svc: SsoService) -> None:
        self._svc = svc
        self._inner: HttpJwks | None = None

    def key(self, issuer: str, kid: str) -> Any:
        if self._inner is None:
            cfg = IssuerConfig(
                issuer, self._svc.cfg.client_id, self._svc.client.metadata()["jwks_uri"]
            )
            self._inner = HttpJwks({issuer: cfg})
        return self._inner.key(issuer, kid)


def build_service(
    c: dict[str, Any],
    store: StateStore,
    session_secret: bytes,
    user_lookup: Any,
    extra_origins: tuple[str, ...] = (),
    http: Any = None,
) -> SsoService:
    try:
        cfg = SsoClientConfig(
            issuer=str(c["issuer"]).rstrip("/"),
            client_id=str(c["client_id"]),
            client_secret=str(c["client_secret"]),
            redirect_uri=str(c["redirect_uri"]),
            scopes=c.get("scopes", "openid profile email"),
            roles_claim=c.get("roles_claim", "groups"),
            name_claim=c.get("name_claim", "preferred_username"),
            session_lifetime_s=int(c.get("session_lifetime_s", 8 * 3600)),
        )
    except (KeyError, ValueError) as e:
        raise SsoSetupError(str(e)) from e
    mapping = AdminSsoConfig(
        IssuerConfig(cfg.issuer, cfg.client_id, ""),
        roles_claim=cfg.roles_claim,
        name_claim=cfg.name_claim,
        group_roles={g: tuple(r) for g, r in (c.get("group_roles") or {}).items()},
        user_lookup=user_lookup,
    )
    return SsoService(cfg, mapping, store, session_secret, http=http, extra_origins=extra_origins)


class LiveSso:
    """The SSO in force right now (None = off) plus the org admin's draft."""

    def __init__(
        self,
        state: StateStore,
        user_lookup: Any,
        secrets: SecretsService | None = None,
        console_url: str | None = None,
        fixed: SsoService | None = None,
        extra_origins: tuple[str, ...] = (),
        http: Any = None,
    ) -> None:
        self._state, self._lookup, self._secrets = state, user_lookup, secrets
        self._console = (console_url or "").rstrip("/")
        self._extra, self._http = extra_origins, http
        self._lock = threading.Lock()
        self.managed_by_install = fixed is not None
        self._active: SsoService | None = fixed
        self._draft: SsoService | None = None
        self._doc: dict[str, Any] = {}
        self._secret_row: str | None = None
        if fixed is None:
            self._load()

    # ------------------------------------------------------------------ state
    def get(self) -> SsoService | None:
        return self._active

    def draft(self) -> SsoService | None:
        return self._draft

    @property
    def redirect_uri(self) -> str | None:
        return f"{self._console}{CALLBACK_PATH}" if self._console else None

    def bearer(self) -> "LiveBearerAuthenticator":
        return LiveBearerAuthenticator(self)

    def _build(self, c: dict[str, Any]) -> SsoService:
        key = bytes.fromhex(self._doc["session_key"])
        return build_service(c, self._state, key, self._lookup, self._extra, self._http)

    def _load(self) -> None:
        if self._secrets is None:
            return
        rows = [s for s in self._secrets.store.all() if s.provider_id == SSO_SECRET_ID]
        if not rows:
            return
        row = max(rows, key=lambda s: s.created_at)
        self._secret_row = row.id
        self._doc = json.loads(self._secrets.reveal(row.id))
        if self._doc.get("draft"):
            self._draft = self._build(self._doc["draft"])
        if self._doc.get("active"):
            self._active = self._build(self._doc["active"])

    def _save(self, by: str) -> None:
        if self._secrets is None:
            raise SsoSetupError("this install has no secret store (CAPTAIN_SECRETS_KEY)")
        old = self._secret_row
        row = self._secrets.put(SSO_SECRET_ID, json.dumps(self._doc), by)
        self._secret_row = row.id
        if old:
            self._secrets.delete(old)

    # ------------------------------------------------------------------ views
    def view(self) -> dict[str, Any]:
        def pub(c: dict[str, Any] | None) -> dict[str, Any] | None:
            if not c:
                return None
            return {
                "provider": c.get("provider", "oidc"),
                "issuer": c["issuer"],
                "client_id": c["client_id"],
                "secret_last4": str(c["client_secret"])[-4:],
                "roles_claim": c.get("roles_claim", "groups"),
                "group_roles": c.get("group_roles") or {},
                "tested_by": c.get("tested_by"),
                "tested_at": c.get("tested_at"),
                "turned_on_by": c.get("turned_on_by"),
                "turned_on_at": c.get("turned_on_at"),
            }

        return {
            "on": self._active is not None,
            "managed_by_install": self.managed_by_install,
            "redirect_uri": self.redirect_uri,
            "can_edit": not self.managed_by_install and self._secrets is not None
            and bool(self._console),
            "active": pub(self._doc.get("active")),
            "draft": pub(self._doc.get("draft")),
        }  # fmt: skip

    # ---------------------------------------------------------------- changes
    def save_draft(self, body: dict[str, Any], by: str) -> None:
        if self.managed_by_install:
            raise SsoSetupError("SSO is managed by this install's settings")
        if not self.redirect_uri:
            raise SsoSetupError("the install's public console address is not set")
        with self._lock:
            prev = self._doc.get("draft") or self._doc.get("active") or {}
            secret = (body.get("client_secret") or "").strip() or prev.get("client_secret")
            if not secret:
                raise SsoSetupError("enter the client secret")
            c = {
                "provider": body.get("provider") or "oidc",
                "issuer": str(body["issuer"]).strip().rstrip("/"),
                "client_id": str(body["client_id"]).strip(),
                "client_secret": secret,
                "redirect_uri": self.redirect_uri,
                "roles_claim": (body.get("roles_claim") or "groups").strip(),
                "group_roles": body.get("group_roles") or {},
            }
            self._doc.setdefault("session_key", pysecrets.token_hex(32))
            draft = self._build(c)  # validates (secret length, https)
            self._doc["draft"] = c
            self._draft = draft
            self._save(by)

    def check_draft(self) -> dict[str, Any]:
        """Can Captain reach the provider? (discovery document)"""
        d = self._draft
        if d is None:
            raise SsoSetupError("save the settings first")
        try:
            meta = d.client.metadata()
        except Exception as e:  # noqa: BLE001 - shown to the admin as a reason
            raise SsoSetupError(f"could not read the provider's settings: {e}") from e
        return {"issuer": meta.get("issuer"), "ok": True}

    def mark_tested(self, who: str) -> None:
        with self._lock:
            if not self._doc.get("draft"):
                return
            self._doc["draft"] = {**self._doc["draft"], "tested_by": who,
                                  "tested_at": datetime.now(UTC).isoformat()}  # fmt: skip
            self._save(who)

    def turn_on(self, by: str) -> None:
        with self._lock:
            c = self._doc.get("draft")
            if not c:
                raise SsoSetupError("save the settings first")
            if not c.get("tested_at"):
                raise SsoSetupError("press Test sign-in and complete it before turning SSO on")
            self._doc["active"] = {**c, "turned_on_by": by,
                                   "turned_on_at": datetime.now(UTC).isoformat()}  # fmt: skip
            self._active = self._build(self._doc["active"])
            self._save(by)

    def turn_off(self, by: str) -> None:
        if self.managed_by_install:
            raise SsoSetupError("SSO is managed by this install's settings")
        with self._lock:
            self._doc.pop("active", None)
            self._active = None
            self._save(by)


class LiveBearerAuthenticator:
    """IdP tokens sent as Bearer credentials, checked against whatever SSO is on now."""

    def __init__(self, live: LiveSso) -> None:
        self._live = live
        self._cache: tuple[SsoService, OidcAdminAuthenticator] | None = None

    def authenticate(self, token: str, client_host: str | None) -> AdminActor:
        svc = self._live.get()
        if svc is None:
            raise AuthenticationError("invalid admin token")
        if self._cache is None or self._cache[0] is not svc:
            self._cache = (svc, OidcAdminAuthenticator(svc.mapping, _DiscoveredJwks(svc)))
        return self._cache[1].authenticate(token, client_host)


def new_test_state() -> str:
    """OIDC `state` for a test sign-in: marked so the callback uses the draft."""
    return "t." + pysecrets.token_urlsafe(24)
