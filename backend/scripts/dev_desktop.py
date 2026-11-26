"""Development only: a fleet key for the Windows desktop agent.

    .venv\\Scripts\\python scripts\\dev_desktop.py

Creates (idempotently, through the audited admin path):
- principal `desktop-fleet` (SERVICE_ACCOUNT, role `desktop-agent`: may send desktop reports)
- AI Register entry `AI-DESKTOP` (Tier 3, A1, NO models and NO tools: the key cannot chat)
- a key saved to backend/.dev/desktop-fleet.key (never printed)

Then: powershell -File ..\\desktop\\windows\\Invoke-CaptainDesktopAgent.ps1
      -CaptainUrl http://127.0.0.1:8000 -KeyFile .dev\\desktop-fleet.key
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from captain.config import build_from_env  # noqa: E402
from captain.core.admin import AdminActor, AdminService  # noqa: E402
from captain.core.context.service import Application  # noqa: E402
from captain.core.identity.model import PrincipalRecord  # noqa: E402
from captain.core.models import AutonomyLevel, Environment, RegisterEntry, RiskTier  # noqa: E402
from captain.devsetup import DEV  # noqa: E402

ACTOR = AdminActor("dev-desktop-script", frozenset({"admin"}))
KEY_FILE = DEV / "desktop-fleet.key"


def main() -> None:
    built, _ = build_from_env()
    infra = built.infra
    admin = AdminService(built.core, built.register, infra.budget_catalog)  # type: ignore[arg-type]
    infra.identity_store.put_principal(
        PrincipalRecord(id="desktop-fleet", type="SERVICE_ACCOUNT", org_id="dev-org",
                        display_name="Desktop agent fleet", team_id="dev-team",
                        roles=frozenset({"desktop-agent"}))
    )  # fmt: skip
    if built.register.get("AI-DESKTOP") is None:
        admin.upsert_register(
            ACTOR,
            RegisterEntry(id="AI-DESKTOP", name="Desktop agent fleet", risk_tier=RiskTier.TIER_3,
                          autonomy=AutonomyLevel.A1, owner_id="dev-owner", cost_centre="CC-DEV",
                          purpose="Reports AI desktop apps and MCP bypasses (metadata only)"),
        )  # fmt: skip
    infra.context_store.put_application(
        Application("desktop-agent-application", "dev-project", "AI-DESKTOP", "Desktop agent")
    )
    if not KEY_FILE.exists():
        issued = admin.issue_key(ACTOR, "desktop-fleet", "AI-DESKTOP", "dev-project",
                                 "desktop-agent-application", Environment.DEVELOPMENT, None,
                                 "desktop fleet")  # fmt: skip
        KEY_FILE.write_text(issued.raw_key, encoding="utf-8")
        print(f"created the desktop fleet key ({KEY_FILE.name})")
    else:
        print("desktop fleet key already exists")


if __name__ == "__main__":
    main()
