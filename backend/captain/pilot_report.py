"""Pilot metrics report (MVP acceptance: "pilot metrics are reported").

    python -m captain.pilot_report --days 14 --out pilot-report.md

Built from the evidence ledger and the alert store through the same queries the dashboard
uses, so the numbers match what people see there. It reports facts and marks each MVP
acceptance criterion as MET (seen in the data), NOT SEEN (the data has no example yet) or
CHECK ELSEWHERE (Captain cannot measure it from its own ledger: it needs the secret scan,
the egress test, the load test or the SSO check, which are run outside).
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from captain.api.admin_queries import AdminQueries

CALL_EVENTS = {"call.allowed", "call.masked", "call.refused", "call.observed", "tool.held"}


def _usd(micros: int) -> str:
    return f"${micros / 1_000_000:,.2f}"


def build_report(q: AdminQueries, days: int = 14, now: datetime | None = None) -> dict[str, Any]:
    until = now or datetime.now(UTC)
    since = until - timedelta(days=days)
    decisions: Counter[str] = Counter()
    refusals: Counter[str] = Counter()
    coverage: Counter[str] = Counter()
    spend = est = tokens_in = tokens_out = 0
    by_model: Counter[str] = Counter()
    for (
        _d,
        etype,
        decision,
        reason,
        cov,
        _risk,
        model,
        source,
        n,
        cost,
        tin,
        tout,
    ) in q.overview_rows(since, until):
        if etype in CALL_EVENTS:
            decisions[decision or "NONE"] += n
            coverage[cov or "GOVERNED"] += n
            if etype == "call.refused":
                refusals[reason or "UNKNOWN"] += n
        elif etype == "usage.settled":
            spend += cost
            est += cost if source != "PROVIDER_REPORTED" else 0
            tokens_in += tin
            tokens_out += tout
            by_model[model or "unknown"] += cost
    channels: dict[str, Counter[str]] = {}
    for cov, channel, _who, n in q.coverage_counts(
        since, until, CALL_EVENTS, {"admin", "security"}
    ):
        channels.setdefault(channel, Counter())[cov] += n
    people = sorted(q.usage_by("principal", since, until), key=lambda r: -r[4])[:10]
    apps = sorted(q.usage_by("app", since, until), key=lambda r: -r[4])[:10]
    lat = q.latency_percentiles(since, until, "latency_ms")
    ttft = q.latency_percentiles(since, until, "time_to_first_token_ms")
    alerts = [a for a in q.alerts(1000) if a.created_at >= since]
    alert_kinds = Counter(a.kind for a in alerts)
    security = sum(
        n
        for p, t, n, _ in q.partition_stats(since, until)
        if p == "security" and t == "call.refused"
    )
    governed = coverage.get("GOVERNED", 0)
    refused = decisions.get("BLOCK", 0)
    metrics: dict[str, Any] = {
        "period": {"from": since.isoformat(), "to": until.isoformat(), "days": days},
        "volume": {
            "governed_decisions": governed,
            "observed_events": coverage.get("OBSERVED", 0),
            "bypass_detected": coverage.get("BYPASS_DETECTED", 0),
            "refused": refused,
            "refusal_rate_pct": round(100 * refused / governed, 1) if governed else 0.0,
            "decisions": dict(decisions),
            "by_channel": {c: dict(v) for c, v in sorted(channels.items())},
        },
        "refusals_by_reason": dict(refusals.most_common()),
        "cost": {
            "spend_micros": spend,
            "estimated_micros": est,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "by_model": dict(by_model.most_common(10)),
            "top_people": [{"key": r[0], "calls": r[1], "cost_micros": r[4]} for r in people],
            "top_applications": [{"key": r[0], "calls": r[1], "cost_micros": r[4]} for r in apps],
        },
        "latency_ms": {"p50": lat[0], "p95": lat[1]},
        "time_to_first_token_ms": {"p50": ttft[0], "p95": ttft[1]},
        "alerts": dict(alert_kinds),
        "security_refusals_unattributed": security,
        "approvals_pending": len(q.approvals("PENDING", 500)),
    }
    metrics["acceptance"] = acceptance(metrics)
    return metrics


def acceptance(m: dict[str, Any]) -> list[dict[str, str]]:
    def row(name: str, status: str, evidence: str) -> dict[str, str]:
        return {"criterion": name, "status": status, "evidence": evidence}

    cap = m["refusals_by_reason"].get("BUDGET_EXHAUSTED", 0)
    thresholds = m["alerts"].get("budget.threshold", 0)
    ttft = m["time_to_first_token_ms"]["p50"]
    return [
        row(
            "Cap refusal with a clear reason",
            "MET" if cap else "NOT SEEN",
            f"{cap} BUDGET_EXHAUSTED refusals in the period",
        ),
        row(
            "80% budget alert",
            "MET" if thresholds else "NOT SEEN",
            f"{thresholds} budget.threshold alerts in the period",
        ),
        row(
            "Streamed workload: time to first token measured",
            "MET" if ttft is not None else "NOT SEEN",
            "no streamed call with a measured first token"
            if ttft is None
            else f"p50 {ttft:.0f} ms",
        ),
        row(
            "Chargeback produced automatically",
            "MET",
            "GET /admin/v1/reports/chargeback (also on the Budgets page); not time-dependent",
        ),
        row(
            "SSO and roles restrict access",
            "CHECK ELSEWHERE",
            "run `python -m captain.sso_check` and sign in as a user in each role "
            "(docs/sso-setup.md)",
        ),
        row(
            "100% of pilot calls go through Captain; no developer-held provider keys",
            "CHECK ELSEWHERE",
            f"needs the secret scan and the egress test (cluster); Captain saw "
            f"{m['volume']['bypass_detected']} bypass signals from discovery logs",
        ),
        row(
            "Latency within target at pilot load",
            "CHECK ELSEWHERE",
            "run `python -m bench.gateway_load` at pilot concurrency; "
            "the figures above include model time",
        ),
        row("Pilot metrics reported", "MET", "this report"),
    ]


def render(m: dict[str, Any]) -> str:
    v, c = m["volume"], m["cost"]
    p = m["period"]
    out = [
        "# Stixis Captain: pilot report",
        f"Period: last {p['days']} days ({p['from'][:10]} to {p['to'][:10]})",
        "",
        "## Volume",
        f"- Governed decisions: **{v['governed_decisions']:,}** "
        f"(refused: {v['refused']:,}, {v['refusal_rate_pct']}%)",
        f"- Observed-only events: {v['observed_events']:,}; "
        f"bypass signals: {v['bypass_detected']:,}",
        f"- Approvals pending now: {m['approvals_pending']}; refusals not tied to a system "
        f"(bad key, retired system): {m['security_refusals_unattributed']}",
        "",
        "| Channel | " + " | ".join(["GOVERNED", "OBSERVED", "BYPASS_DETECTED"]) + " |",
        "|---|---|---|---|",
        *(
            f"| {ch} | {n.get('GOVERNED', 0)} | {n.get('OBSERVED', 0)} "
            f"| {n.get('BYPASS_DETECTED', 0)} |"
            for ch, n in v["by_channel"].items()
        ),
        "",
        "## Why calls were refused",
        *(f"- {k}: {n:,}" for k, n in m["refusals_by_reason"].items()),
        "",
        "## Cost",
        f"- Spend: **{_usd(c['spend_micros'])}** ({_usd(c['estimated_micros'])} of it estimated, "
        "not provider-reported)",
        f"- Tokens: {c['tokens_in']:,} in / {c['tokens_out']:,} out",
        *(f"- {k}: {_usd(x)}" for k, x in c["by_model"].items()),
        "",
        "Top people by spend: "
        + (", ".join(f"{r['key']} ({_usd(r['cost_micros'])})" for r in c["top_people"]) or "none"),
        "",
        "Top applications by spend: "
        + (
            ", ".join(f"{r['key']} ({_usd(r['cost_micros'])})" for r in c["top_applications"])
            or "none"
        ),
        "",
        "## Latency (Captain + model)",
        f"- Total call time: p50 {_ms(m['latency_ms']['p50'])}, p95 {_ms(m['latency_ms']['p95'])}",
        f"- Time to first token (streamed): p50 {_ms(m['time_to_first_token_ms']['p50'])}, "
        f"p95 {_ms(m['time_to_first_token_ms']['p95'])}",
        "",
        "## MVP acceptance (M1a)",
        "| Criterion | Status | Evidence |",
        "|---|---|---|",
        *(f"| {r['criterion']} | {r['status']} | {r['evidence']} |" for r in m["acceptance"]),
        "",
        "MET = seen in the data. NOT SEEN = no example in this period yet. CHECK ELSEWHERE = "
        "cannot be measured from Captain's own ledger.",
    ]
    return "\n".join(out) + "\n"


def _ms(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0f} ms"


def main(argv: list[str] | None = None) -> int:
    from pathlib import Path

    from sqlalchemy import create_engine

    from captain.api.admin_queries import PgAdminQueries, ResolvingQueries
    from captain.config import load_settings
    from captain.core.subjects import Pseudonymiser
    from captain.db.stores import PgSubjectStore

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    engine = create_engine(load_settings().database_url)
    # The ledger holds pseudonyms for people (ADR-005): show them as people again.
    q = ResolvingQueries(
        PgAdminQueries(engine), Pseudonymiser(PgSubjectStore(engine), lambda _: True)
    )
    text = render(build_report(q, a.days))
    if a.out:
        a.out.write_text(text, encoding="utf-8")
        print(f"wrote {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
