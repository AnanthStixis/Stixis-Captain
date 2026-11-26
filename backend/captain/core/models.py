"""Captain Core contract types.

These types are the frozen contract between Captain Core and every integration
(API Gateway, MCP Proxy, IDE Connectors, Browser Extension, SDK Libraries,
Hooks & Telemetry, Desktop Agent). See docs/adr/0002-core-decision-api.md.

Rules encoded here:
- Money is always integer micro-units of the billing currency (never float).
- Content is never required: callers may send only a keyed content hash.
- Every decision carries an explicit reason code (CTO F14).
- Every usage record states where its numbers came from (reported/estimated/observed).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

MicroUnits = Annotated[int, Field(ge=0, description="Money in micro-units (1 USD = 1_000_000)")]


def _now() -> datetime:
    return datetime.now(UTC)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- enums


class Channel(StrEnum):
    """Integration-layer channel the request arrived through."""

    API = "API"
    SDK = "SDK"
    MCP = "MCP"
    IDE = "IDE"
    BROWSER = "BROWSER"
    HOOK = "HOOK"
    DESKTOP = "DESKTOP"


class CoverageClass(StrEnum):
    """How much control Captain actually had over the event (honest coverage model)."""

    GOVERNED = "GOVERNED"  # routed through Captain, policy enforced before the action
    OBSERVED = "OBSERVED"  # reported after the fact by telemetry; no pre-enforcement
    BYPASS_DETECTED = "BYPASS_DETECTED"  # reliable evidence of an unapproved route
    UNOBSERVED = "UNOBSERVED"  # no evidence either way — never means "no usage"


class ActionType(StrEnum):
    MODEL_CALL = "MODEL_CALL"
    TOOL_CALL = "TOOL_CALL"
    WEB_SUBMIT = "WEB_SUBMIT"


class DecisionType(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    ALLOW_WITH_REDACTION = "ALLOW_WITH_REDACTION"
    ALLOW_WITH_LIMITS = "ALLOW_WITH_LIMITS"
    OBSERVE_ONLY = "OBSERVE_ONLY"


class RiskTier(StrEnum):
    """From the AI Register (Stixis AIDLC). Tier 1 is the most critical."""

    TIER_1 = "TIER_1"
    TIER_2 = "TIER_2"
    TIER_3 = "TIER_3"
    TIER_4 = "TIER_4"

    @property
    def fails_closed(self) -> bool:
        """Tier 1–2 fail closed when a control cannot run; Tier 3–4 fail open + log."""
        return self in (RiskTier.TIER_1, RiskTier.TIER_2)


class AutonomyLevel(StrEnum):
    A0 = "A0"
    A1 = "A1"
    A2 = "A2"
    A3 = "A3"
    A4 = "A4"


class RiskLevel(StrEnum):
    """Per-request risk (Risk & Decision Service), distinct from the system's tier."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class PrincipalType(StrEnum):
    USER = "USER"
    SERVICE_ACCOUNT = "SERVICE_ACCOUNT"
    APPLICATION = "APPLICATION"
    AGENT = "AGENT"


class Environment(StrEnum):
    DEVELOPMENT = "DEVELOPMENT"
    QA = "QA"
    PRODUCTION = "PRODUCTION"


class UsageSource(StrEnum):
    PROVIDER_REPORTED = "PROVIDER_REPORTED"
    ESTIMATED = "ESTIMATED"
    OBSERVED = "OBSERVED"  # imported from vendor telemetry
    UNAVAILABLE = "UNAVAILABLE"


class ApprovalState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"  # approval used once; cannot be replayed (CTO F7)
    CANCELLED = "CANCELLED"  # e.g. by kill switch


class ReasonCode(StrEnum):
    """Every decision names exactly one primary reason. Never a generic fallback."""

    ALLOWED = "ALLOWED"
    AUTH_INVALID = "AUTH_INVALID"
    KEY_REVOKED = "KEY_REVOKED"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    CONTEXT_UNRESOLVED = "CONTEXT_UNRESOLVED"
    MODEL_NOT_ALLOWED = "MODEL_NOT_ALLOWED"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    TOOL_ARGUMENTS_INVALID = "TOOL_ARGUMENTS_INVALID"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    BUDGET_NOT_CONFIGURED = "BUDGET_NOT_CONFIGURED"
    RATE_LIMITED = "RATE_LIMITED"
    PII_REDACTED = "PII_REDACTED"
    INJECTION_DETECTED = "INJECTION_DETECTED"
    TOOL_NOT_REGISTERED = "TOOL_NOT_REGISTERED"
    MODEL_NOT_REGISTERED = "MODEL_NOT_REGISTERED"
    MODEL_NOT_PRICED = "MODEL_NOT_PRICED"
    NO_ROUTE = "NO_ROUTE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    OUTPUT_BLOCKED = "OUTPUT_BLOCKED"
    SECRET_DETECTED = "SECRET_DETECTED"  # noqa: S105 — a reason code, not a secret
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"
    CONTROL_UNAVAILABLE = "CONTROL_UNAVAILABLE"  # fail-closed path
    CONTROL_UNAVAILABLE_FAIL_OPEN = "CONTROL_UNAVAILABLE_FAIL_OPEN"
    OBSERVED_ONLY = "OBSERVED_ONLY"


class EventType(StrEnum):
    """Evidence ledger event types (CTO review, Problem 4 schema)."""

    CALL_ALLOWED = "call.allowed"
    CALL_REFUSED = "call.refused"
    CALL_MASKED = "call.masked"
    CALL_OBSERVED = "call.observed"
    USAGE_SETTLED = "usage.settled"
    USAGE_RELEASED = "usage.released"
    TOOL_HELD = "tool.held"
    TOOL_EXECUTED = "tool.executed"
    APPROVAL_DECIDED = "approval.decided"
    BUDGET_OVERRIDE = "budget.override"
    POLICY_CHANGED = "policy.changed"
    KEY_ISSUED = "key.issued"
    KEY_REVOKED = "key.revoked"
    KEY_ROTATED = "key.rotated"
    KILL_SWITCH = "killswitch.changed"
    REGISTER_CHANGED = "register.changed"
    BUDGET_DEFINED = "budget.defined"
    REGISTRY_CHANGED = "registry.changed"
    ALERT_RAISED = "alert.raised"
    APPROVAL_ESCALATED = "approval.escalated"
    EVALUATION_SCORED = "evaluation.scored"
    EVALUATION_FAILED = "evaluation.failed"
    SUBJECT_ERASED = "subject.erased"


# ----------------------------------------------------------------- identity/context


class Credential(_Frozen):
    """What an integration presents. Raw secrets are never logged or persisted."""

    kind: str = Field(pattern="^(virtual_key|workload_oidc|session)$")
    secret: str = Field(repr=False, min_length=1)


class ContextHints(_Frozen):
    """Optional hints from the channel. Core validates them; it never trusts them blindly."""

    project_id: str | None = None
    application_id: str | None = None
    environment: Environment | None = None
    session_id: str | None = None
    device_id: str | None = None
    client_ip: str | None = None


class Principal(_Frozen):
    id: str
    type: PrincipalType
    org_id: str
    team_id: str | None = None
    roles: frozenset[str] = frozenset()


class RegisterEntry(_Frozen):
    """Governance context owned by the AI Register (source of truth, CTO F8)."""

    id: str
    name: str
    risk_tier: RiskTier
    autonomy: AutonomyLevel
    owner_id: str
    cost_centre: str
    purpose: str
    allowed_models: frozenset[str] = frozenset()
    allowed_tools: frozenset[str] = frozenset()
    active: bool = True
    eu_ai_act_high_risk: bool = False
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _high_risk_floor(self) -> RegisterEntry:
        # A system flagged high-risk can never run below Tier 2 controls (CTO Problem 5).
        if self.eu_ai_act_high_risk and self.risk_tier in (RiskTier.TIER_3, RiskTier.TIER_4):
            raise ValueError("EU AI Act high-risk systems must be Tier 1 or Tier 2")
        return self


class ResolvedContext(_Frozen):
    principal: Principal
    register_entry: RegisterEntry
    project_id: str
    application_id: str
    environment: Environment
    key_id: str | None = None


# --------------------------------------------------------------------------- decide


class ContentRef(_Frozen):
    """Content to inspect. Either inline text (inspected, never stored by default)
    or only a keyed hash when the channel may not send content."""

    text: str | None = Field(default=None, repr=False)
    content_hmac: str | None = None
    trusted: bool = True  # False for retrieved docs, tool results, web pages

    @model_validator(mode="after")
    def _one_of(self) -> ContentRef:
        if self.text is None and self.content_hmac is None:
            raise ValueError("ContentRef needs text or content_hmac")
        return self


class GovernanceRequest(_Frozen):
    request_id: UUID = Field(default_factory=uuid4)
    trace_id: str
    received_at: datetime = Field(default_factory=_now)
    context: ResolvedContext
    channel: Channel
    action: ActionType
    coverage: CoverageClass = CoverageClass.GOVERNED
    model: str | None = None
    tool: str | None = None
    tool_arguments: dict[str, object] | None = None
    contents: tuple[ContentRef, ...] = ()
    max_output_tokens: int | None = Field(default=None, gt=0)
    streaming: bool = False

    @model_validator(mode="after")
    def _target(self) -> GovernanceRequest:
        if self.action is ActionType.MODEL_CALL and not self.model:
            raise ValueError("MODEL_CALL requires model")
        if self.action is ActionType.TOOL_CALL and not self.tool:
            raise ValueError("TOOL_CALL requires tool")
        return self


class FindingType(StrEnum):
    PII = "PII"
    SECRET = "SECRET"  # noqa: S105 — a finding type, not a secret
    INJECTION = "INJECTION"
    UNSAFE = "UNSAFE"
    BANNED_TERM = "BANNED_TERM"
    SIZE_LIMIT = "SIZE_LIMIT"


class GuardrailFinding(_Frozen):
    """What a detector found. Never contains the matched text itself."""

    type: FindingType
    entity: str  # e.g. EMAIL_ADDRESS, AADHAAR, AWS_ACCESS_KEY, prompt_injection
    detector: str  # e.g. regex, presidio, guard:granite
    content_index: int = Field(ge=0)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    score: float = Field(ge=0.0, le=1.0, default=1.0)


class Limits(_Frozen):
    max_output_tokens: int | None = None
    streaming_mode: str | None = None  # buffer | chunk | passthrough_async_check | passthrough
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None


class Decision(_Frozen):
    request_id: UUID
    trace_id: str
    decision: DecisionType
    reason_code: ReasonCode
    explanation: str
    principal_id: str = ""
    register_id: str = ""
    channel: Channel = Channel.API
    model: str | None = None
    policy_bundle: str
    policy_version: str
    matched_policy_ids: tuple[str, ...] = ()
    risk_tier: RiskTier
    limits: Limits = Limits()
    redacted_contents: tuple[ContentRef, ...] | None = None
    approval_ticket: UUID | None = None
    fail_open_applied: bool = False
    findings: tuple[GuardrailFinding, ...] = ()
    risk_level: RiskLevel | None = None
    risk_score: int | None = Field(default=None, ge=0, le=100)
    risk_factors: tuple[str, ...] = ()
    budget_scopes: tuple[str, ...] = ()  # e.g. ("org:o1", "team:t1", "key:k1"); set by Core
    decided_at: datetime = Field(default_factory=_now)

    @property
    def permits_execution(self) -> bool:
        return self.decision in (
            DecisionType.ALLOW,
            DecisionType.ALLOW_WITH_REDACTION,
            DecisionType.ALLOW_WITH_LIMITS,
        )


# --------------------------------------------------------------------------- budget


class CostEstimate(_Frozen):
    """Upper-bound cost: input tokens × input price + max_output_tokens × output price."""

    input_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(ge=0)
    amount: MicroUnits
    price_version: str


class Reservation(_Frozen):
    reservation_id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    amount: MicroUnits
    budget_keys: tuple[str, ...]  # every level reserved atomically
    expires_at: datetime
    trace_id: str = ""
    principal_id: str = ""
    register_id: str = ""
    channel: Channel = Channel.API
    model: str | None = None
    scopes: tuple[str, ...] = ()  # attribution (org, cost centre, team, project, app, …)
    fail_open_applied: bool = False


class TokenUsage(_Frozen):
    input_tokens: int = Field(ge=0, default=0)
    output_tokens: int = Field(ge=0, default=0)
    cached_input_tokens: int = Field(ge=0, default=0)
    reasoning_tokens: int = Field(ge=0, default=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ActualUsage(_Frozen):
    usage: TokenUsage
    source: UsageSource
    cost: MicroUnits
    price_version: str
    latency_ms: int | None = Field(default=None, ge=0)
    time_to_first_token_ms: int | None = Field(default=None, ge=0)
    provider: str | None = None
    succeeded: bool = True
    error_code: str | None = None


class Settlement(_Frozen):
    reservation_id: UUID
    reserved: MicroUnits
    charged: MicroUnits
    released: MicroUnits


# ------------------------------------------------------------------------- approval


class ApprovalTicket(_Frozen):
    ticket_id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    state: ApprovalState
    arguments_hash: str  # binds approval to exact parameters (CTO F7)
    requested_by: str = ""
    register_id: str = ""
    channel: Channel = Channel.MCP
    action_summary: str = ""  # e.g. "MCP jira.create_issue"; never raw arguments
    policy_version: str = ""
    assigned_approvers: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=_now)
    escalated_at: datetime | None = None
    approver_id: str | None = None
    expires_at: datetime
    decided_at: datetime | None = None
    reason: str | None = None


# --------------------------------------------------------------------------- record


class UsageEvent(_Frozen):
    """Anything an integration reports: a settled governed call or an observed event."""

    event_id: UUID = Field(default_factory=uuid4)
    request_id: UUID | None = None
    trace_id: str
    occurred_at: datetime = Field(default_factory=_now)
    event_type: EventType
    channel: Channel
    coverage: CoverageClass
    principal_id: str | None
    register_id: str | None
    decision: DecisionType | None = None
    reason_code: ReasonCode | None = None
    policy_version: str | None = None
    model: str | None = None
    tool: str | None = None
    usage: ActualUsage | None = None
    payload_hmac: str | None = None  # keyed hash; content itself is never in the event
    # Structured, non-sensitive facts (admin action parameters, alert data). Never
    # secrets, raw keys or prompt content.
    details: dict[str, str | int | float | bool | None] | None = None


class LedgerReceipt(_Frozen):
    """Proof that an event was durably accepted (queued) for the evidence ledger."""

    event_id: UUID
    partition: str
    accepted_at: datetime


# --------------------------------------------------------------- gateway support


class ProviderTarget(_Frozen):
    """Where an approved model call goes. Resolved by the Core from the Model/Tool
    Registry; carries the NAME of the credential, never the credential."""

    alias: str
    provider_id: str
    provider_kind: str  # openai | anthropic | gemini | azure_openai | bedrock | …
    provider_model: str
    credential_ref: str
    base_url: str | None = None


class ToolDescriptor(_Frozen):
    """A tool an MCP client may see: only what the registry and the register entry allow."""

    name: str
    description: str
    input_schema: dict[str, object]
    read_only: bool
    impact: str  # low | medium | high


class ToolTarget(_Frozen):
    """Where an approved tool call goes. Carries the NAME of the credential, never it."""

    tool: str
    server_id: str
    upstream_name: str  # the tool's name on the upstream server
    url: str
    transport: str
    credential_ref: str | None = None


class ToolOutputVerdict(_Frozen):
    """Result of screening a tool's output before it reaches the agent. `entities` names
    what was found (never the matched text). `blocked` outputs carry no text."""

    text: str = Field(repr=False)
    blocked: bool = False
    masked: bool = False
    entities: tuple[str, ...] = ()


class ScopeUsage(_Frozen):
    """Spend against one budget for the current period (money in Captain micro-units)."""

    scope: str  # e.g. principal:u1, app:a1, project:p1, org:o1
    period: str  # DAILY | MONTHLY
    limit: int
    spent: int
    reserved: int  # held by calls still in flight

    @property
    def remaining(self) -> int:
        return max(self.limit - self.spent - self.reserved, 0)


class PricedUsage(_Frozen):
    usage: TokenUsage
    cost: MicroUnits
    price_version: str
    source: UsageSource
