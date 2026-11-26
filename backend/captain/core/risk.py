"""Per-request risk assessment (Risk & Decision Service).

The register tier says how critical the SYSTEM is. This scores the individual
REQUEST, so two calls from the same Tier 3 system can differ: one plain summary
request, one with a secret in the prompt and an injected instruction in a retrieved
document trying to call a high-impact tool.

Deterministic and explainable by design (Master Prompt: no LLM for rule decisions):
a fixed, versioned table of factors with points, summed and capped at 100. Each
decision carries the level, the score and the factors that produced it; policy can
use the level, and the ledger records it.
"""

from __future__ import annotations

from dataclasses import dataclass

from captain.core.models import (
    ActionType,
    AutonomyLevel,
    Environment,
    FindingType,
    GovernanceRequest,
    GuardrailFinding,
    RiskLevel,
    RiskTier,
)

RISK_MODEL_VERSION = "risk-1.0"

_TIER = {RiskTier.TIER_1: 40, RiskTier.TIER_2: 30, RiskTier.TIER_3: 15, RiskTier.TIER_4: 5}
_AUTONOMY_TOOL = {AutonomyLevel.A3: 15, AutonomyLevel.A4: 25}
_TOOL_RISK = {"LOW": 0, "MEDIUM": 5, "HIGH": 15, "CRITICAL": 25}


@dataclass(frozen=True, slots=True)
class ToolFacts:
    impact: str  # low | high
    risk: str  # LOW | MEDIUM | HIGH | CRITICAL
    read_only: bool


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    level: RiskLevel
    score: int
    factors: tuple[str, ...]
    model_version: str = RISK_MODEL_VERSION


def level_for(score: int) -> RiskLevel:
    if score >= 75:
        return RiskLevel.CRITICAL
    if score >= 50:
        return RiskLevel.HIGH
    if score >= 25:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def assess(
    request: GovernanceRequest,
    findings: tuple[GuardrailFinding, ...],
    tool: ToolFacts | None,
    injection_threshold: float = 0.5,
) -> RiskAssessment:
    entry = request.context.register_entry
    pts: list[tuple[str, int]] = [(f"tier:{entry.risk_tier.value}", _TIER[entry.risk_tier])]
    if entry.eu_ai_act_high_risk:
        pts.append(("eu_ai_act_high_risk", 10))
    if request.context.environment is Environment.PRODUCTION:
        pts.append(("environment:production", 5))

    if request.action is ActionType.TOOL_CALL:
        if a := _AUTONOMY_TOOL.get(entry.autonomy):
            pts.append((f"autonomy:{entry.autonomy.value}", a))
        if tool is None:
            pts.append(("tool:unregistered", 20))
        else:
            if tool.impact == "high" and not tool.read_only:
                pts.append(("tool:high_impact", 20))
            if t := _TOOL_RISK.get(tool.risk, 0):
                pts.append((f"tool_risk:{tool.risk}", t))

    kinds = {f.type for f in findings}
    if FindingType.SECRET in kinds:
        pts.append(("finding:secret", 30))
    inj = [
        f for f in findings if f.type is FindingType.INJECTION and f.score >= injection_threshold
    ]
    if inj:
        pts.append(("finding:injection", 25))
        if any(f.entity == "indirect_prompt_injection" for f in inj):
            pts.append(("finding:indirect_injection", 5))
    if FindingType.UNSAFE in kinds:
        pts.append(("finding:unsafe", 20))
    if FindingType.PII in kinds:
        pts.append(("finding:personal_data", 10))
    if any(not c.trusted for c in request.contents):
        pts.append(("content:untrusted_source", 5))

    score = min(sum(p for _, p in pts), 100)
    return RiskAssessment(level_for(score), score, tuple(f"{n}(+{p})" for n, p in pts))
