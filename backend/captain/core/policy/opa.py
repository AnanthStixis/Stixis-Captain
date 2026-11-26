"""OPA Policy Engine client.

Captain never decides access rules in Python. It builds the input document, asks OPA
(`POST /v1/data/captain/decision/result`) and validates the answer. If OPA is down,
slow, or returns something malformed, `PolicyUnavailableError` is raised and the
engine applies the tier fail mode (Tier 1–2 closed, Tier 3–4 open + logged).
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from captain.core.models import (
    DecisionType,
    GovernanceRequest,
    GuardrailFinding,
    ReasonCode,
)
from captain.core.risk import RiskAssessment, ToolFacts

DECISION_PATH = "/v1/data/captain/decision/result"


class PolicyUnavailableError(Exception):
    pass


class PolicyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    decision: DecisionType
    reason_code: ReasonCode
    explanation: str
    policy_ids: tuple[str, ...]
    policy_bundle: str
    policy_version: str
    limits: dict[str, Any] = {}


class PolicyEngine(Protocol):
    def evaluate(self, payload: dict[str, Any]) -> PolicyResult: ...


def build_input(
    request: GovernanceRequest,
    findings: tuple[GuardrailFinding, ...],
    tool: ToolFacts | None = None,
    risk: RiskAssessment | None = None,
) -> dict[str, Any]:
    """The OPA input contract. Content never goes to OPA — only finding metadata."""
    ctx = request.context
    entry = ctx.register_entry
    return {
        "request": {
            "channel": request.channel.value,
            "action": request.action.value,
            "coverage": request.coverage.value,
            "model": request.model,
            "tool": request.tool,
            "max_output_tokens": request.max_output_tokens,
            "streaming": request.streaming,
        },
        "principal": {
            "id": ctx.principal.id,
            "type": ctx.principal.type.value,
            "roles": sorted(ctx.principal.roles),
            "team_id": ctx.principal.team_id,
        },
        "register": {
            "id": entry.id,
            "tier": entry.risk_tier.value,
            "autonomy": entry.autonomy.value,
            "allowed_models": sorted(entry.allowed_models),
            "allowed_tools": sorted(entry.allowed_tools),
            "high_risk": entry.eu_ai_act_high_risk,
        },
        "project_id": ctx.project_id,
        "application_id": ctx.application_id,
        "environment": ctx.environment.value,
        # Registry facts and per-request risk; policies may use either.
        "tool": {"impact": tool.impact, "risk": tool.risk, "read_only": tool.read_only}
        if tool
        else None,
        "risk": {"level": risk.level.value, "score": risk.score} if risk else None,
        "findings": [
            {"type": f.type.value, "entity": f.entity, "score": f.score} for f in findings
        ],
    }


class OpaPolicyEngine:
    def __init__(self, base_url: str, timeout_s: float = 0.2, client: httpx.Client | None = None):
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout_s)

    def data(self, path: str) -> Any:
        """Read a document from the policy data OPA has loaded (for example "tiers" or
        "bundle"): what is actually ACTIVE, not what is in a file."""
        try:
            resp = self._client.get(f"/v1/data/captain/{path.strip('/')}")
            resp.raise_for_status()
            return resp.json().get("result")
        except (httpx.HTTPError, ValueError) as e:
            raise PolicyUnavailableError(f"OPA request failed: {e}") from e

    def evaluate(self, payload: dict[str, Any]) -> PolicyResult:
        try:
            resp = self._client.post(DECISION_PATH, json={"input": payload})
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise PolicyUnavailableError(f"OPA request failed: {e}") from e
        if "result" not in body:
            # Undefined result means the bundle is missing or broken — never "allow".
            raise PolicyUnavailableError("OPA returned no result (policy not loaded?)")
        try:
            return PolicyResult.model_validate(body["result"])
        except ValidationError as e:
            raise PolicyUnavailableError(f"OPA result does not match contract: {e}") from e
