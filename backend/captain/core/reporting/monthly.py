"""Monthly governance report and board-pack inputs (FR-17), plus XLSX export.

All figures come from the evidence ledger, so the report, the chargeback and the
audit evidence always agree. Costs marked ESTIMATED are reported separately from
provider-reported costs; nothing is presented as more exact than it is.
"""

from __future__ import annotations

import io
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font

from captain.core.ledger.chain import LedgerEntry

INCIDENT_REASONS = {
    "INJECTION_DETECTED",
    "SECRET_DETECTED",
    "KILL_SWITCH_ACTIVE",
    "TOOL_ARGUMENTS_INVALID",
}


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)
    return start, end


def monthly_report(
    entries: list[LedgerEntry], year: int, month: int, top_n: int = 5
) -> dict[str, Any]:
    start, end = month_bounds(year, month)
    es = [e for e in entries if start <= e.recorded_at < end]
    decisions = Counter(e.body.get("decision") for e in es if e.event_type.startswith("call."))
    refusals = Counter(e.body.get("reason_code") for e in es if e.event_type == "call.refused")
    spend_total = spend_est = calls_settled = 0
    spend_by_model: Counter[str] = Counter()
    spend_by_cc: Counter[str] = Counter()
    for e in es:
        if e.event_type != "usage.settled":
            continue
        u = e.body.get("usage") or {}
        cost = int(u.get("cost") or 0)
        calls_settled += 1
        spend_total += cost
        if u.get("source") != "PROVIDER_REPORTED":
            spend_est += cost
        spend_by_model[str(e.body.get("model") or "unknown")] += cost
        scopes = dict(
            s.split(":", 1)
            for s in str((e.body.get("details") or {}).get("scopes") or "").split(",")
            if ":" in s
        )
        spend_by_cc[scopes.get("cost_centre", "unattributed")] += cost
    risk = Counter(
        (e.body.get("details") or {}).get("risk_level")
        for e in es
        if e.event_type.startswith("call.")
    )
    per_system: dict[str, Counter[str]] = defaultdict(Counter)
    for e in es:
        if e.event_type == "call.refused":
            per_system[e.partition]["refusals"] += 1
        if (e.body.get("details") or {}).get("risk_level") in ("HIGH", "CRITICAL"):
            per_system[e.partition]["high_risk_calls"] += 1
    ranked = sorted(
        per_system.items(), key=lambda kv: (-(kv[1]["high_risk_calls"] + kv[1]["refusals"]), kv[0])
    )
    top_risks = [
        {"register_id": k, "high_risk_calls": v["high_risk_calls"], "refusals": v["refusals"]}
        for k, v in ranked[:top_n]
    ]
    return {
        "period": f"{year:04d}-{month:02d}",
        "generated_at": datetime.now(UTC).isoformat(),
        "governed_calls": sum(v for k, v in decisions.items() if k),
        "observed_events": sum(1 for e in es if e.event_type == "call.observed"),
        "decisions": dict(decisions),
        "refusals_by_reason": dict(refusals.most_common()),
        "incidents": sum(v for k, v in refusals.items() if k in INCIDENT_REASONS),
        "fail_open_calls": sum(
            1 for e in es if (e.body.get("details") or {}).get("fail_open") is True
        ),
        "risk_levels": {k: v for k, v in risk.items() if k},
        "approvals": {
            "held": sum(1 for e in es if e.event_type == "tool.held"),
            "decided": sum(1 for e in es if e.event_type == "approval.decided"),
            "escalated": sum(1 for e in es if e.event_type == "approval.escalated"),
        },
        "kill_switch_changes": sum(1 for e in es if e.event_type == "killswitch.changed"),
        "budget_overrides": sum(1 for e in es if e.event_type == "budget.override"),
        "alerts": dict(
            Counter(
                (e.body.get("details") or {}).get("kind")
                for e in es
                if e.event_type == "alert.raised"
            )
        ),
        "spend": {
            "total_micros": spend_total,
            "estimated_micros": spend_est,
            "settled_calls": calls_settled,
            "by_model": dict(spend_by_model.most_common()),
            "by_cost_centre": dict(spend_by_cc.most_common()),
        },
        "top_risk_systems": top_risks,
    }


def to_xlsx(sheets: dict[str, list[dict[str, Any]]]) -> bytes:
    """One worksheet per entry; header row in bold. Values are written as data, never
    as formulas (a leading '=' is escaped) to prevent spreadsheet formula injection."""
    wb = Workbook()
    wb.remove(wb.active)  # type: ignore[arg-type]
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name[:31])
        if not rows:
            continue
        headers = list(rows[0])
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append([_safe(r.get(h)) for h in headers])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _safe(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    if isinstance(v, dict | list):
        return str(v)
    return v


def report_sheets(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    flat = {k: v for k, v in report.items() if not isinstance(v, dict | list)}
    return {
        "Summary": [{"metric": k, "value": v} for k, v in flat.items()],
        "Refusals": [{"reason": k, "count": v} for k, v in report["refusals_by_reason"].items()],
        "Spend by model": [
            {"model": k, "micros": v} for k, v in report["spend"]["by_model"].items()
        ],
        "Spend by cost centre": [
            {"cost_centre": k, "micros": v} for k, v in report["spend"]["by_cost_centre"].items()
        ],
        "Top risk systems": report["top_risk_systems"],
    }
