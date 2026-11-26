"""Role-based access control for administrative operations.

Roles come from the client IdP via SSO group mapping (FR-02). Every admin API
endpoint calls `require()`; hiding UI navigation is never the control.
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    KEYS_READ = "keys:read"
    KEYS_WRITE = "keys:write"
    REGISTER_READ = "register:read"
    REGISTER_WRITE = "register:write"
    POLICY_READ = "policy:read"
    POLICY_PUBLISH = "policy:publish"
    BUDGET_READ = "budget:read"
    BUDGET_WRITE = "budget:write"
    BUDGET_OVERRIDE = "budget:override"
    USAGE_READ = "usage:read"
    APPROVAL_DECIDE = "approval:decide"
    KILL_SWITCH = "killswitch:write"
    AUDIT_READ = "audit:read"
    AUDIT_EXPORT = "audit:export"
    SUBJECT_ERASE = "subjects:erase"  # erase a person from the evidence (ADR-005)
    CONTENT_READ = "content:read"  # prompt/response content; off unless explicitly granted


ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "admin": frozenset(p for p in Permission if p is not Permission.CONTENT_READ),
    "approver": frozenset(
        {Permission.APPROVAL_DECIDE, Permission.USAGE_READ, Permission.REGISTER_READ}
    ),
    "finops": frozenset(
        {
            Permission.BUDGET_READ,
            Permission.BUDGET_WRITE,
            Permission.BUDGET_OVERRIDE,
            Permission.USAGE_READ,
        }
    ),
    "auditor": frozenset(
        {
            Permission.AUDIT_READ,
            Permission.AUDIT_EXPORT,
            Permission.USAGE_READ,
            Permission.POLICY_READ,
            Permission.REGISTER_READ,
        }
    ),
    "viewer": frozenset({Permission.USAGE_READ, Permission.REGISTER_READ, Permission.BUDGET_READ}),
    "content_reviewer": frozenset({Permission.CONTENT_READ, Permission.USAGE_READ}),
}


class PermissionDenied(Exception):
    def __init__(self, permission: Permission) -> None:
        super().__init__(f"missing permission: {permission}")
        self.permission = permission


def permissions_for(roles: frozenset[str]) -> frozenset[Permission]:
    out: set[Permission] = set()
    for r in roles:
        out |= ROLE_PERMISSIONS.get(r, frozenset())  # unknown roles grant nothing
    return frozenset(out)


def require(roles: frozenset[str], permission: Permission) -> None:
    if permission not in permissions_for(roles):
        raise PermissionDenied(permission)
