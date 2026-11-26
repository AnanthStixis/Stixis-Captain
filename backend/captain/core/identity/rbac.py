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
    USERS_WRITE = "users:write"  # add people, change their console roles, deactivate them
    AIKEYS_WRITE = "aikeys:write"  # provider (AI) keys held by Captain
    ORG_WRITE = "org:write"  # company profile and onboarding
    COMPANIES_WRITE = "companies:write"  # Stixis only: client companies (control plane)


# The company's own administrator: everything the install offers. Content stays a separate,
# explicit grant. `admin` (the original role name) is an alias, so existing IdP group
# mappings keep working.
# Managing other companies is Stixis's job, never a client org admin's.
_ORG_ADMIN = frozenset(
    p for p in Permission if p not in (Permission.CONTENT_READ, Permission.COMPANIES_WRITE)
)


ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "org-admin": _ORG_ADMIN,
    "admin": _ORG_ADMIN,
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
    # Stixis staff on the Stixis install (CAPTAIN_CONTROL_PLANE=1): the client company list.
    # Companies, Stixis staff (users) and the audit trail of those changes; nothing else.
    "platform-admin": frozenset(
        {
            Permission.COMPANIES_WRITE,
            Permission.USERS_WRITE,
            Permission.KEYS_READ,
            Permission.AUDIT_READ,
        }
    ),  # fmt: skip
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
