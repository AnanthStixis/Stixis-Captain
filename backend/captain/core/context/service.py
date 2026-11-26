"""Project Context: AI Register access, project/application registry, and request
context resolution.

The AI Register is the source of truth for tier, autonomy, owner and purpose (CTO F8).
`LocalRegisterProvider` stands in until the Stixis AI Register API exists; a remote
provider implements the same protocol. Changes that weaken controls (higher tier
number, lower autonomy, removing the high-risk flag) require an approval reference
and raise an alert.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from captain.core.bus import EventBus, Topic
from captain.core.decision_api import ContextUnresolvedError
from captain.core.identity.model import KeyRecord, PrincipalRecord
from captain.core.models import (
    AutonomyLevel,
    ContextHints,
    Environment,
    Principal,
    PrincipalType,
    RegisterEntry,
    ResolvedContext,
    RiskTier,
)

_TIER_RANK = {RiskTier.TIER_1: 1, RiskTier.TIER_2: 2, RiskTier.TIER_3: 3, RiskTier.TIER_4: 4}
_AUTONOMY_RANK = {a: i for i, a in enumerate(AutonomyLevel)}


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    org_id: str
    name: str
    team_id: str | None = None
    active: bool = True


@dataclass(frozen=True, slots=True)
class Application:
    id: str
    project_id: str
    register_id: str
    name: str
    environments: frozenset[Environment] = frozenset(Environment)
    active: bool = True


class ControlDowngradeError(Exception):
    """A register change would weaken controls without a recorded approval."""


class RegisterProvider(Protocol):
    def get(self, register_id: str) -> RegisterEntry | None: ...


class RegisterStore(Protocol):
    def get(self, register_id: str) -> RegisterEntry | None: ...
    def put(self, entry: RegisterEntry) -> None: ...


class InMemoryRegisterStore:
    def __init__(self) -> None:
        self._entries: dict[str, RegisterEntry] = {}

    def get(self, register_id: str) -> RegisterEntry | None:
        return self._entries.get(register_id)

    def put(self, entry: RegisterEntry) -> None:
        self._entries[entry.id] = entry


class LocalRegisterProvider:
    def __init__(
        self,
        bus: EventBus,
        on_downgrade: Callable[[RegisterEntry, RegisterEntry], None],
        store: RegisterStore | None = None,
    ):
        self._store: RegisterStore = store or InMemoryRegisterStore()
        self._bus = bus
        self._on_downgrade = on_downgrade
        self._lock = threading.Lock()

    def get(self, register_id: str) -> RegisterEntry | None:
        return self._store.get(register_id)

    def upsert(self, entry: RegisterEntry, approval_ref: str | None = None) -> RegisterEntry:
        with self._lock:
            old = self._store.get(entry.id)
            if old is not None:
                if is_control_downgrade(old, entry):
                    if not approval_ref:
                        raise ControlDowngradeError(
                            f"{entry.id}: weakening tier/autonomy/flags needs an approval"
                        )
                    self._on_downgrade(old, entry)
                entry = entry.model_copy(update={"version": old.version + 1})
            self._store.put(entry)
        self._bus.publish(Topic.REGISTER_CHANGED, entry.id)
        return entry


def is_control_downgrade(old: RegisterEntry, new: RegisterEntry) -> bool:
    return (
        _TIER_RANK[new.risk_tier] > _TIER_RANK[old.risk_tier]
        or _AUTONOMY_RANK[new.autonomy] < _AUTONOMY_RANK[old.autonomy]
        or (old.eu_ai_act_high_risk and not new.eu_ai_act_high_risk)
    )


class ContextStore(Protocol):
    def project(self, project_id: str) -> Project | None: ...
    def application(self, application_id: str) -> Application | None: ...
    def put_project(self, p: Project) -> None: ...
    def put_application(self, a: Application) -> None: ...


class InMemoryContextStore:
    def __init__(self) -> None:
        self.projects: dict[str, Project] = {}
        self.applications: dict[str, Application] = {}

    def project(self, project_id: str) -> Project | None:
        return self.projects.get(project_id)

    def application(self, application_id: str) -> Application | None:
        return self.applications.get(application_id)

    def put_project(self, p: Project) -> None:
        self.projects[p.id] = p

    def put_application(self, a: Application) -> None:
        self.applications[a.id] = a


class ContextService:
    def __init__(self, register: RegisterProvider, store: ContextStore) -> None:
        self._register = register
        self._store = store

    def resolve(
        self, key: KeyRecord, principal: PrincipalRecord, hints: ContextHints
    ) -> ResolvedContext:
        # A key is bound to exactly one project/application/environment. Hints may only
        # confirm the binding; a conflicting hint is refused, never silently re-mapped.
        for hinted, bound, name in (
            (hints.project_id, key.project_id, "project"),
            (hints.application_id, key.application_id, "application"),
            (hints.environment, key.environment, "environment"),
        ):
            if hinted is not None and hinted != bound:
                raise ContextUnresolvedError(f"{name} hint conflicts with key binding")

        entry = self._register.get(key.register_id)
        if entry is None or not entry.active:
            raise ContextUnresolvedError("AI Register entry missing or retired")
        project = self._store.project(key.project_id)
        if project is None or not project.active or project.org_id != principal.org_id:
            raise ContextUnresolvedError("project missing, inactive or in another org")
        app = self._store.application(key.application_id)
        if app is None or not app.active or app.project_id != project.id:
            raise ContextUnresolvedError("application missing, inactive or not in project")
        if app.register_id != entry.id:
            raise ContextUnresolvedError("application and key point to different register entries")
        if key.environment not in app.environments:
            raise ContextUnresolvedError("environment not enabled for application")

        return ResolvedContext(
            principal=Principal(
                id=principal.id,
                type=PrincipalType(principal.type),
                org_id=principal.org_id,
                team_id=principal.team_id or project.team_id,
                roles=principal.roles,
            ),
            register_entry=entry,
            project_id=project.id,
            application_id=app.id,
            environment=key.environment,
            key_id=key.key_id,
        )
