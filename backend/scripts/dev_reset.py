# ruff: noqa: E501
"""Put the DEV environment back to a known test state, or show it.

    .venv\\Scripts\\python scripts/dev_reset.py --check       show the state (changes nothing)
    .venv\\Scripts\\python scripts/dev_reset.py                reset to the baseline below
    .venv\\Scripts\\python scripts/dev_reset.py --arm-c5       reset, then set a tiny budget for test C5

Baseline = what every test in docs/testing-guide.md assumes:
  * no kill switches on channels API / MCP / BROWSER (an IDE switch is left alone unless --all)
  * AI register entry AI-DEV: active, Tier 3, allowed models [claude-sonnet, vscode]
  * monthly budget on principal:dev-app = $50
  * no rate limits
It uses CAPTAIN_DEV_ADMIN_TOKEN from backend/.env (development only) and the Admin API, so every
change is recorded in the evidence ledger like any other admin action (weakening changes use the
approval reference "dev-reset"). It never touches PostgreSQL directly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from captain.envfile import DEFAULT_ENV_FILE, dotenv_values  # noqa: E402

ADMIN = "http://127.0.0.1:8100/admin/v1"
GATEWAY = "http://127.0.0.1:8000"
BASELINE_MODELS = ["claude-sonnet", "vscode"]
BASELINE_LIMIT = 50_000_000
http = httpx.Client(timeout=30)


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {dotenv_values(DEFAULT_ENV_FILE)['CAPTAIN_DEV_ADMIN_TOKEN']}"}


def call(method: str, path: str, **kw: object) -> httpx.Response:
    return http.request(method, ADMIN + path, headers=admin_headers(), **kw)  # type: ignore[arg-type]


def state() -> dict[str, object]:
    reg = next(e for e in call("GET", "/register").json() if e["id"] == "AI-DEV")
    key = (BACKEND / ".dev" / "dev-app.key").read_text().strip()
    usage = http.get(f"{GATEWAY}/v1/usage", headers={"Authorization": f"Bearer {key}"}).json()
    own = next((b for b in usage["budgets"] if b["scope"] == "principal:dev-app"), None)
    return {
        "kill_switches": [
            (k["scope"], k["target"], k["reason"]) for k in call("GET", "/killswitch").json()
        ],
        "ai_dev": (reg["active"], reg["risk_tier"], sorted(reg["allowed_models"])),
        "budget": None
        if own is None
        else {"limit": own["limit"], "spent": own["spent"], "remaining": own["remaining"]},
        "rate_limits": [r["scope"] for r in call("GET", "/rate-limits").json()],
        "pending_approvals": len(call("GET", "/approvals?state=PENDING").json()),
    }


def show(s: dict[str, object]) -> None:
    print("kill switches :", s["kill_switches"] or "none")
    active, tier, models = s["ai_dev"]  # type: ignore[misc]
    print(f"AI-DEV        : active={active} tier={tier} models={models}")
    b = s["budget"]
    print(
        "dev-app budget:",
        "none"
        if b is None
        else f"limit {b['limit']:,} spent {b['spent']:,} remaining {b['remaining']:,} (micro-units)",
    )  # type: ignore[index]
    print("rate limits   :", s["rate_limits"] or "none")
    print("pending approvals:", s["pending_approvals"])


def reset(include_ide_switch: bool) -> None:
    for k in call("GET", "/killswitch").json():
        if k["scope"] == "channel" and k["target"] == "IDE" and not include_ide_switch:
            continue
        r = call(
            "POST",
            "/killswitch/release",
            json={"scope": k["scope"], "target": k["target"], "reason": "dev-reset"},
        )
        print(f"released kill switch {k['scope']}:{k['target']} -> {r.status_code}")
    reg = next(e for e in call("GET", "/register").json() if e["id"] == "AI-DEV")
    want = {**reg, "active": True, "risk_tier": "TIER_3", "allowed_models": BASELINE_MODELS}
    if {k: reg[k] for k in ("active", "risk_tier")} != {
        "active": True,
        "risk_tier": "TIER_3",
    } or sorted(reg["allowed_models"]) != BASELINE_MODELS:
        r = call("PUT", "/register/AI-DEV", json={"entry": want, "approval_ref": "dev-reset"})
        print("AI-DEV restored ->", r.status_code)
    for rl in call("GET", "/rate-limits").json():
        r = call("DELETE", "/rate-limits", params={"scope": rl["scope"]})
        print(f"lifted rate limit {rl['scope']} -> {r.status_code}")
    r = call(
        "PUT",
        "/budgets",
        json={"scope": "principal:dev-app", "period": "MONTHLY", "limit_micros": BASELINE_LIMIT},
    )
    print("budget restored to $50 ->", r.status_code)


def arm_c5() -> None:
    spent = state()["budget"]["spent"]  # type: ignore[index]
    limit = int(spent) + 11_000  # type: ignore[call-overload]
    r = call(
        "PUT",
        "/budgets",
        json={"scope": "principal:dev-app", "period": "MONTHLY", "limit_micros": limit},
    )
    print(
        f"C5 armed: limit {limit:,} micro-units (spent {spent:,}, about $0.011 of headroom) -> {r.status_code}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--arm-c5", action="store_true")
    ap.add_argument(
        "--all", action="store_true", help="also release a kill switch on the IDE channel"
    )
    a = ap.parse_args()
    try:
        if not a.check:
            reset(a.all)
            if a.arm_c5:
                arm_c5()
        print()
        show(state())
    except (httpx.HTTPError, KeyError, StopIteration) as e:
        print(
            f"cannot reach the services or read the state ({type(e).__name__}). Are they running? .\\scripts\\dev_up.ps1"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
