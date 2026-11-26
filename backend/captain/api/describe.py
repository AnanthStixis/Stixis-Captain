"""Plain-language descriptions of evidence events, for people who are not engineers.

Every event shown on the dashboard (Activity, Audit, request detail, CSV exports) gets:
- `headline`: one sentence saying what happened ("ravi@corp's request to claude-sonnet was
  blocked."),
- `outcome` + `outcome_tone`: a short result word and a colour (Allowed, Blocked, ...),
- `why`: the reason in everyday words (None when there is nothing to explain),
- `category`: what kind of thing it was (AI request, Tool use, Cost, Settings change, ...),
- `who`: the person, application or agent, as a reader would say it.
The raw codes stay available as "technical details"; nothing here changes the evidence.

Every EventType and ReasonCode has wording; a test enumerates both, so a new code cannot
reach the dashboard without it.
"""

from __future__ import annotations

from typing import Any

from captain.core.models import EventType, ReasonCode

REASONS: dict[str, str] = {
    ReasonCode.ALLOWED: "Allowed by the rules for this AI system.",
    ReasonCode.AUTH_INVALID: "The Captain key was missing, wrong or unknown.",
    ReasonCode.KEY_REVOKED: "The Captain key has been revoked or has expired.",
    ReasonCode.KILL_SWITCH_ACTIVE: "An emergency stop (kill switch) is switched on.",
    ReasonCode.CONTEXT_UNRESOLVED: (
        "Captain could not tell which AI system or project this belongs to "
        "(the system may have been retired)."
    ),
    ReasonCode.MODEL_NOT_ALLOWED: "This AI model is not approved for this system.",
    ReasonCode.TOOL_NOT_ALLOWED: "This tool is not approved for this system.",
    ReasonCode.TOOL_ARGUMENTS_INVALID: "The tool was called with input that does not match "
    "what the tool accepts.",
    ReasonCode.BUDGET_EXHAUSTED: "The spending limit (budget) has been reached.",
    ReasonCode.BUDGET_NOT_CONFIGURED: "No budget covers this system, so spending is not "
    "allowed yet.",
    ReasonCode.RATE_LIMITED: "Too many requests in a short time.",
    ReasonCode.PII_REDACTED: "Personal data was hidden before it reached the AI.",
    ReasonCode.INJECTION_DETECTED: "The text looked like an attempt to trick the AI into "
    "ignoring its rules (prompt injection).",
    ReasonCode.TOOL_NOT_REGISTERED: "This tool is not registered in Captain.",
    ReasonCode.MODEL_NOT_REGISTERED: "This AI model is not registered in Captain.",
    ReasonCode.MODEL_NOT_PRICED: "This AI model has no price set, so its cost cannot be "
    "controlled.",
    ReasonCode.NO_ROUTE: "No AI provider is set up for this model.",
    ReasonCode.PROVIDER_ERROR: "The AI provider failed or could not be reached.",
    ReasonCode.OUTPUT_BLOCKED: "The AI's answer contained something that is not allowed, "
    "so it was stopped.",
    ReasonCode.SECRET_DETECTED: "A secret (such as a password or access key) was found in "
    "the text.",
    ReasonCode.APPROVAL_REQUIRED: "This action needs a person's approval first.",
    ReasonCode.CONTENT_TOO_LARGE: "The request was too large.",
    ReasonCode.CONTROL_UNAVAILABLE: "A safety check could not run, so Captain refused to be "
    "safe.",
    ReasonCode.CONTROL_UNAVAILABLE_FAIL_OPEN: "A safety check could not run. This lower-risk "
    "system was allowed to continue, and that was recorded.",
    ReasonCode.OBSERVED_ONLY: "Reported after the fact; Captain did not control it.",
}

# Result word and colour (Mantine colour names) for the dashboard.
OUTCOMES: dict[str, tuple[str, str]] = {
    "allowed": ("Allowed", "green"),
    "hidden": ("Allowed, data hidden", "teal"),
    "blocked": ("Blocked", "red"),
    "waiting": ("Waiting for approval", "yellow"),
    "recorded": ("Recorded only", "gray"),
    "bypass": ("Possible bypass", "orange"),
    "charged": ("Charged", "blue"),
    "returned": ("Not charged", "gray"),
    "ran": ("Done", "green"),
    "change": ("Settings change", "violet"),
    "alert": ("Alert", "orange"),
    "approved": ("Approved", "green"),
    "rejected": ("Rejected", "red"),
    "expired": ("Expired", "gray"),
    "scored": ("Scored", "blue"),
    "low_score": ("Low score", "orange"),
    "failed": ("Failed", "red"),
    "erased": ("Erased", "violet"),
}

# Wording per event type; only the category, the rest is built in describe().
CATEGORIES: dict[str, str] = {
    EventType.CALL_ALLOWED: "AI request",
    EventType.CALL_REFUSED: "AI request",
    EventType.CALL_MASKED: "AI request",
    EventType.CALL_OBSERVED: "Seen outside Captain",
    EventType.USAGE_SETTLED: "Cost",
    EventType.USAGE_RELEASED: "Cost",
    EventType.TOOL_HELD: "Tool use",
    EventType.TOOL_EXECUTED: "Tool use",
    EventType.APPROVAL_DECIDED: "Approval",
    EventType.APPROVAL_ESCALATED: "Approval",
    EventType.BUDGET_OVERRIDE: "Settings change",
    EventType.POLICY_CHANGED: "Settings change",
    EventType.KEY_ISSUED: "Settings change",
    EventType.KEY_REVOKED: "Settings change",
    EventType.KEY_ROTATED: "Settings change",
    EventType.KILL_SWITCH: "Emergency stop",
    EventType.REGISTER_CHANGED: "Settings change",
    EventType.BUDGET_DEFINED: "Settings change",
    EventType.REGISTRY_CHANGED: "Settings change",
    EventType.ALERT_RAISED: "Alert",
    EventType.EVALUATION_SCORED: "Answer quality",
    EventType.EVALUATION_FAILED: "Answer quality",
    EventType.SUBJECT_ERASED: "Privacy",
    EventType.AIKEY_CHANGED: "Settings change",
    EventType.COMPANY_CHANGED: "Companies",
    "user.changed": "Users",
    "org.changed": "Company settings",
    "invite.created": "Users",
    "invite.redeemed": "Users",
}

# What the guardrails found, in words (detector entity -> phrase).
FOUND: dict[str, str] = {
    "EMAIL_ADDRESS": "an email address",
    "PHONE_NUMBER": "a phone number",
    "CREDIT_CARD": "a card number",
    "IBAN_CODE": "a bank account number",
    "IP_ADDRESS": "an IP address",
    "PERSON": "a person's name",
    "LOCATION": "a location",
    "US_SSN": "a social security number",
    "IN_AADHAAR": "an Aadhaar number",
    "IN_PAN": "a PAN number",
    "PASSPORT": "a passport number",
    "prompt_injection": "a prompt-injection attempt",
}

SCOPES: dict[str, str] = {
    "org": "the whole organisation",
    "team": "team",
    "cost_centre": "cost centre",
    "project": "project",
    "app": "application",
    "principal": "person",
    "key": "key",
    "model": "model",
    "tool": "tool",
}


def money(micros: int | None) -> str:
    """Captain money is in millionths of a dollar."""
    if micros is None:
        return ""
    usd = micros / 1_000_000
    return f"${usd:,.4f}" if usd < 0.01 else f"${usd:,.2f}"


GENERIC_WHO = ("someone not identified", "an unrecognised user")
APP_NAMES = {"chatgpt": "ChatGPT", "claude": "Claude Desktop", "cursor": "Cursor",
             "vscode": "VS Code"}  # fmt: skip


def who_label(principal: Any) -> str:
    """As used inside a sentence (generic phrases in lower case)."""
    p = str(principal or "")
    if not p or p in ("unattributed", "None"):
        return GENERIC_WHO[0]
    if p.startswith("vendor-unmapped:"):
        return GENERIC_WHO[1]
    if p == "captain":
        return "Captain"
    return p


def scope_label(scope: Any) -> str:
    """`principal:ravi` -> "person ravi"; `ptool:ravi:jira.x` -> "person ravi using tool jira.x"."""
    s = str(scope or "")
    kind, sep, rest = s.partition(":")
    if not sep:
        return s
    if kind == "ptool":
        person, _, tool = rest.partition(":")
        return f"person {person} using tool {tool}"
    if kind == "org":
        return "the whole organisation"
    return f"{SCOPES.get(kind, kind)} {rest}"


def found_label(detected: Any) -> str | None:
    """`PII:EMAIL_ADDRESS x1, PII:PHONE_NUMBER x2` -> "an email address and a phone number"."""
    if not detected:
        return None
    phrases: list[str] = []
    for part in str(detected).split(","):
        entity = part.strip().split(" ")[0].split(":")[-1]
        if not entity:
            continue
        phrase = FOUND.get(entity, entity.replace("_", " ").lower())
        if phrase not in phrases:
            phrases.append(phrase)
    if not phrases:
        return None
    return phrases[0] if len(phrases) == 1 else ", ".join(phrases[:-1]) + " and " + phrases[-1]


def _target(v: dict[str, Any]) -> str:
    if v.get("tool"):
        return f"use of the tool {v['tool']}"
    if v.get("channel") == "BROWSER":
        return "message on an AI website"
    if v.get("model"):
        return f"request to {v['model']}"
    return "request"


def _possessive(who: str) -> str:
    return f"{who}'" if who.endswith("s") else f"{who}'s"


def _result(key: str, headline: str, category: str, who: str, why: str | None) -> dict[str, Any]:
    word, tone = OUTCOMES[key]
    if who in GENERIC_WHO:  # a generic phrase starting the sentence gets its capital back
        who = who[0].upper() + who[1:]
        if headline.lower().startswith(who.lower()):
            headline = who + headline[len(who) :]
    return {
        "headline": headline,
        "outcome": word,
        "outcome_tone": tone,
        "category": category,
        "who": who,
        "why": why,
    }


def describe(v: dict[str, Any]) -> dict[str, Any]:
    """`v` is an event as the Admin API shows it (event_view): already resolved to people."""
    et = str(v.get("event_type") or "")
    d: dict[str, Any] = v.get("details") or {}
    who = who_label(v.get("principal_id"))
    cat = CATEGORIES.get(et, "Other")
    reason = v.get("reason_code")
    why = REASONS.get(str(reason)) if reason else None
    found = found_label(d.get("detected"))
    system = v.get("register_id") or "an AI system"

    if et == EventType.CALL_ALLOWED:
        if d.get("fail_open"):
            return _result(
                "allowed",
                f"{_possessive(who)} {_target(v)} was allowed, but a safety check was skipped.",
                cat, who, REASONS[ReasonCode.CONTROL_UNAVAILABLE_FAIL_OPEN],
            )  # fmt: skip
        return _result("allowed", f"{_possessive(who)} {_target(v)} was allowed.", cat, who, None)
    if et == EventType.CALL_MASKED:
        hid = f" {found[0].upper() + found[1:]} was hidden first." if found else ""
        return _result(
            "hidden",
            f"{_possessive(who)} {_target(v)} was allowed after personal data was hidden.{hid}",
            cat, who, why,
        )  # fmt: skip
    if et == EventType.CALL_REFUSED:
        extra = f" It contained {found}." if found else ""
        if reason == ReasonCode.APPROVAL_REQUIRED:
            return _result(
                "waiting", f"{_possessive(who)} {_target(v)} needs approval.", cat, who, why
            )
        return _result(
            "blocked", f"{_possessive(who)} {_target(v)} was blocked.{extra}", cat, who, why
        )
    if et == EventType.CALL_OBSERVED:
        return _observed(v, d, who, cat)
    if et == EventType.USAGE_SETTLED:
        cost = money(v.get("cost_micros"))
        est = " (estimated)" if v.get("usage_source") not in (None, "PROVIDER_REPORTED") else ""
        what = v.get("model") or v.get("tool") or "AI"
        return _result(
            "charged", f"{_possessive(who)} {what} use cost {cost}{est}.", cat, who, None
        )
    if et == EventType.USAGE_RELEASED:
        return _result(
            "returned",
            f"Budget held for {_possessive(who)} request was returned; it did not complete.",
            cat, who, None,
        )  # fmt: skip
    if et == EventType.TOOL_HELD:
        return _result(
            "waiting",
            f"{_possessive(who)} use of the tool {v.get('tool')} is waiting for approval.",
            cat, who, REASONS[ReasonCode.APPROVAL_REQUIRED],
        )  # fmt: skip
    if et == EventType.TOOL_EXECUTED:
        ok = str(d.get("outcome") or "ok") == "ok"
        return _result(
            "ran" if ok else "failed",
            f"{who} ran the tool {v.get('tool')}"
            + ("." if ok else ", but the tool reported an error."),
            cat, who, None,
        )  # fmt: skip
    if et in (EventType.APPROVAL_DECIDED, EventType.APPROVAL_ESCALATED):
        return _approval(et, d, who, cat)
    if et == EventType.ALERT_RAISED:
        return _result("alert", f"Alert: {d.get('subject') or d.get('kind')}.", cat, who, None)
    if et == EventType.EVALUATION_SCORED:
        low = bool(d.get("low"))
        return _result(
            "low_score" if low else "scored",
            f"An answer from {system} was scored {d.get('overall')} out of 1 by the quality "
            "judge" + (" (below the alert level)." if low else "."),
            cat, who, None,
        )  # fmt: skip
    if et == EventType.EVALUATION_FAILED:
        return _result(
            "failed",
            f"The quality judge could not score an answer from {system}.",
            cat, who, "The judge model was unavailable or answered in the wrong format.",
        )  # fmt: skip
    if et == EventType.SUBJECT_ERASED:
        return _result(
            "erased",
            f"{who} erased a person's identity from the records (data-protection request).",
            cat, who, d.get("reason"),
        )  # fmt: skip
    return _settings(et, v, d, who, cat)


def _observed(v: dict[str, Any], d: dict[str, Any], who: str, cat: str) -> dict[str, Any]:
    vendor = str(d.get("vendor") or "")
    if vendor == "desktop:inventory":
        app = APP_NAMES.get(str(d.get("v_app")), d.get("v_app"))
        state = "running" if d.get("v_running") else "installed"
        return _result("recorded", f"{app} was {state} on a computer used by {who}.", cat, who,
                       REASONS[ReasonCode.OBSERVED_ONLY])  # fmt: skip
    if v.get("coverage") == "BYPASS_DETECTED":
        server = d.get("v_server")
        app = APP_NAMES.get(str(d.get("v_app")), d.get("v_app")) or "An AI app"
        where = d.get("v_remote_host") or "a program on the computer"
        return _result(
            "bypass",
            f"{app} is set up to use the tool server \"{server}\" ({where}) without going "
            "through Captain.",
            cat, who, "Tools used this way are not checked or recorded by Captain.",
        )  # fmt: skip
    source = vendor.replace("_", " ") or "Another tool"
    return _result(
        "recorded",
        f"{source} reported AI use by {who} for {d.get('period') or 'this period'}.",
        cat, who, REASONS[ReasonCode.OBSERVED_ONLY],
    )  # fmt: skip


def _approval(et: str, d: dict[str, Any], who: str, cat: str) -> dict[str, Any]:
    if et == EventType.APPROVAL_ESCALATED:
        return _result(
            "waiting",
            "An approval request was overdue and was sent to backup approvers.",
            cat, who, None,
        )  # fmt: skip
    # The event names the person who DECIDED (approved / rejected, and still on a used
    # approval); for an expired or cancelled request it names the person who asked.
    state = str(d.get("state") or "").upper()
    text = {
        "APPROVED": ("approved", f"{who} approved an action that was waiting for approval."),
        "REJECTED": ("rejected", f"{who} rejected an action that was waiting for approval."),
        "CONSUMED": ("ran", f"An action approved by {who} ran (an approval works once)."),
        "EXPIRED": ("expired", f"An approval request from {who} expired before anyone decided."),
        "CANCELLED": (
            "rejected",
            f"An approval request from {who} was cancelled by an " "emergency stop.",
        ),
    }.get(state, ("waiting", f"An approval request from {who} is waiting for a decision."))
    return _result(text[0], text[1], cat, who, d.get("reason"))


def _settings(et: str, v: dict[str, Any], d: dict[str, Any], who: str, cat: str) -> dict[str, Any]:
    reason = d.get("reason")
    if et == EventType.KEY_ISSUED:
        text = f"{who} created a Captain key for {d.get('principal_id')}."
    elif et == EventType.KEY_REVOKED:
        text = f"{who} revoked the Captain key {d.get('key_id')}."
    elif et == EventType.KEY_ROTATED:
        text = f"{who} replaced the Captain key {d.get('old_key_id')} with {d.get('new_key_id')}."
    elif et == EventType.KILL_SWITCH:
        on = d.get("action") == "activate"
        target = f"{d.get('scope')} \"{d.get('target')}\""
        text = f"{who} turned {'ON' if on else 'off'} an emergency stop for {target}."
        return _result("alert" if on else "change", text, cat, who, reason)
    elif et == EventType.REGISTER_CHANGED:
        weaker = " Controls were weakened." if d.get("downgrade") else ""
        retired = " It is retired." if d.get("active") is False else ""
        text = (
            f"{who} updated the AI system {v.get('register_id')} "
            f"(risk {str(d.get('tier') or '').replace('TIER_', 'tier ')}).{weaker}{retired}"
        )
    elif et == EventType.BUDGET_DEFINED:
        kind = d.get("kind")
        scope = scope_label(d.get("scope"))
        if kind == "rate_limit":
            parts = []
            if d.get("requests_per_minute"):
                parts.append(f"{d['requests_per_minute']} requests a minute")
            if d.get("tokens_per_minute"):
                parts.append(f"{d['tokens_per_minute']:,} tokens a minute")
            text = f"{who} set a limit of {' and '.join(parts)} for {scope}."
        elif kind == "rate_limit_removed":
            text = f"{who} removed the rate limit for {scope}."
        else:
            period = str(d.get("period") or "").lower()
            text = f"{who} set a {period} budget of {money(d.get('limit'))} for {scope}."
    elif et == EventType.BUDGET_OVERRIDE:
        text = (
            f"{who} gave {scope_label(d.get('scope'))} an extra {money(d.get('extra'))} of budget."
        )
    elif et == EventType.POLICY_CHANGED:
        text = f"Policy version {d.get('version')} was put into use."
    elif et == "user.changed":
        user = d.get("user") or d.get("email") or d.get("principal_id") or "a user"
        action = str(d.get("action") or "updated")
        roles = d.get("roles")
        extra = f" Roles: {roles}." if roles and action != "deactivated" else ""
        text = f"{who} {action} the user {user}.{extra}"
    elif et == EventType.COMPANY_CHANGED:
        company = d.get("company") or "a company"
        action = str(d.get("action") or "updated")
        text = f"{who} {action} the client company {company}."
    elif et == EventType.AIKEY_CHANGED:
        provider = d.get("provider_id") or "a provider"
        action = {"added": "added", "replaced": "replaced", "removed": "removed"}.get(
            str(d.get("action")), "changed"
        )
        text = f"{who} {action} the company AI key for {provider}."
    elif et == "org.changed":
        text = f"{who} updated the company settings."
    elif et == "invite.created":
        user = d.get("email") or d.get("user") or d.get("principal_id") or "an administrator"
        text = f"{who} created a sign-in invitation for {user}."
    elif et == "invite.redeemed":
        if d.get("how") == "email link":
            text = f"{who} signed in with an emailed sign-in link."
        else:
            text = f"{who} signed in for the first time with an invitation."
    elif et == EventType.REGISTRY_CHANGED:
        text = f"{who} changed the {d.get('kind')} \"{d.get('key')}\" in Models & tools."
    else:
        text = f"{who}: {et.replace('.', ' ')}."
    return _result("change", text, cat, who, reason)


# Alerts: a plain title and what to do, per alert kind.
ALERTS: dict[str, tuple[str, str]] = {
    "budget.threshold": (
        "Budget nearly used up",
        "Check who is spending on Usage by..., then raise the budget on Budgets or ask the "
        "team to slow down. At 100% calls are refused.",
    ),
    "budget.override": (
        "Budget increased by hand",
        "Someone gave extra budget. Check the reason is expected.",
    ),
    "register.downgrade": (
        "Safety controls weakened for an AI system",
        "An AI system was moved to a lower-risk tier or more autonomy. Make sure this was "
        "approved.",
    ),
    "approval.escalated": (
        "Approval request overdue",
        "An action is waiting for approval. Open Approvals and decide it.",
    ),
    "evaluation.low_score": (
        "Low answer quality",
        "The quality judge scored an answer below the alert level. Review the AI system's "
        "recent use on Agents & systems.",
    ),
    "ledger.lag": (
        "Evidence recording is delayed",
        "The background worker is falling behind. Ask your platform team to check it.",
    ),
    "import.failed": (
        "Usage import failed",
        "Usage reported by another tool (such as Copilot) could not be imported. Check its "
        "settings and access.",
    ),
}


def describe_alert(kind: str) -> dict[str, str]:
    title, todo = ALERTS.get(kind, (kind.replace(".", " ").replace("_", " ").capitalize(), ""))
    return {"title": title, "what_to_do": todo}
