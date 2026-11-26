from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from openpyxl import load_workbook

from captain.core.identity.killswitch import KillScope
from captain.core.ledger.chain import Ed25519Signer, KeyRingVerifier
from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.models import (
    ActualUsage,
    AutonomyLevel,
    ContentRef,
    ContextHints,
    CostEstimate,
    Credential,
    PrincipalType,
    RiskTier,
    TokenUsage,
    UsageSource,
)
from captain.core.reporting.evidence import (
    FRAMEWORKS,
    EvidencePackage,
    export_evidence,
    verify_package,
)
from captain.core.reporting.monthly import monthly_report, report_sheets, to_xlsx
from tests.conftest import make_context, model_call, tool_call
from tests.harness.adapters import CaptainCoreHarness

NOW = datetime.now(UTC)
START, END = NOW - timedelta(days=1), NOW + timedelta(days=1)


@pytest.fixture(scope="module")
def ledger() -> tuple[CaptainCoreHarness, InMemoryLedgerStore]:
    h = CaptainCoreHarness()
    h.register_key(
        "svc", make_context("svc", RiskTier.TIER_2, AutonomyLevel.A2, PrincipalType.APPLICATION)
    )
    h.register_key(
        "agent", make_context("agent", RiskTier.TIER_2, AutonomyLevel.A3, PrincipalType.AGENT)
    )
    svc = h.resolve(Credential(kind="virtual_key", secret="svc"), ContextHints())
    agent = h.resolve(Credential(kind="virtual_key", secret="agent"), ContextHints())
    c = h.core
    for _ in range(3):
        d = c.decide(model_call(svc))
        r = c.reserve(
            d, CostEstimate(input_tokens=1, max_output_tokens=1, amount=500, price_version="p")
        )
        c.settle(
            r,
            ActualUsage(
                usage=TokenUsage(input_tokens=1, output_tokens=1),
                source=UsageSource.PROVIDER_REPORTED,
                cost=200,
                price_version="p",
                provider="anthropic",
            ),
        )
    c.decide(model_call(svc, model="forbidden"))
    c.decide(
        model_call(svc).model_copy(
            update={"contents": (ContentRef(text="key AKIAABCDEFGHIJKLMNOP"),)}
        )
    )
    req = tool_call(agent, {"po": 1})
    t = c.request_approval(req, c.decide(req))
    c.decide_approval(t.ticket_id, True, "fin-1", "ok")
    c.kill(KillScope.MODEL, "x", "test", "admin")
    c.m.ledger.write_pending()
    c.m.ledger.anchor()
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    return h, store


def verifier(h: CaptainCoreHarness) -> KeyRingVerifier:
    s = h.built.infra.signer
    assert isinstance(s, Ed25519Signer)
    return KeyRingVerifier({s.key_id: s.public_key()})


@pytest.mark.parametrize("fw", FRAMEWORKS)
def test_export_verifies_for_each_framework(ledger, fw: str) -> None:  # type: ignore[no-untyped-def]
    h, store = ledger
    pkg = export_evidence(store.rows, fw, START, END)
    m = pkg.manifest
    assert m["events"] == len(store.rows) and m["unanchored_events"] == 0 and m["anchor_ids"]
    rows = pkg.files["controls.csv"].decode().splitlines()
    assert rows[0].startswith("control_id,control,evidence_events")
    assert verify_package(pkg, verifier(h)).ok


def test_eu_ai_act_controls_have_evidence(ledger) -> None:  # type: ignore[no-untyped-def]
    _, store = ledger
    pkg = export_evidence(store.rows, "EU_AI_ACT", START, END)
    counts = {
        r.split(",")[0]: int(r.split(",")[-3])
        for r in pkg.files["controls.csv"].decode().splitlines()[1:]
    }
    assert counts["ART12"] > 0 and counts["ART14"] > 0  # logging + human oversight evidenced


def test_tampered_package_detected(ledger) -> None:  # type: ignore[no-untyped-def]
    h, store = ledger
    pkg = export_evidence(store.rows, "NIST_AI_RMF", START, END)
    lines = pkg.files["events.jsonl"].decode().splitlines()
    first = json.loads(lines[0])
    first["body"]["principal_id"] = "someone-else"
    forged = ("\n".join([json.dumps(first, sort_keys=True), *lines[1:]]) + "\n").encode()
    # Attacker also rewrites the manifest's file hash: entry hash + signature still catch it.
    import hashlib

    m2 = {
        **pkg.manifest,
        "files": {**pkg.manifest["files"], "events.jsonl": hashlib.sha256(forged).hexdigest()},
    }
    res = verify_package(EvidencePackage(m2, {**pkg.files, "events.jsonl": forged}), verifier(h))
    assert not res.ok and any("entry hash mismatch" in p for p in res.problems)
    res2 = verify_package(
        EvidencePackage(pkg.manifest, {**pkg.files, "events.jsonl": forged}), verifier(h)
    )
    assert any("SHA-256" in p for p in res2.problems)


def test_export_rejects_unknown_framework(ledger) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        export_evidence(ledger[1].rows, "SOX", START, END)


def test_monthly_report(ledger) -> None:  # type: ignore[no-untyped-def]
    _, store = ledger
    r = monthly_report(store.rows, NOW.year, NOW.month)
    assert r["spend"]["total_micros"] == 600 and r["spend"]["settled_calls"] == 3
    assert r["spend"]["by_cost_centre"] == {"CC-100": 600}
    # Tier 2 with no guard model configured fails closed on content (CONTROL_UNAVAILABLE).
    assert r["refusals_by_reason"] == {"MODEL_NOT_ALLOWED": 1, "CONTROL_UNAVAILABLE": 1}
    assert r["approvals"]["held"] == 1 and r["approvals"]["decided"] == 1
    assert r["kill_switch_changes"] == 1 and r["governed_calls"] == 5
    assert r["top_risk_systems"][0]["register_id"] in ("AI-svc", "AI-agent")
    assert sum(r["risk_levels"].values()) == r["governed_calls"]


def test_xlsx_export_and_formula_injection_escaped(ledger) -> None:  # type: ignore[no-untyped-def]
    _, store = ledger
    sheets = report_sheets(monthly_report(store.rows, NOW.year, NOW.month))
    sheets["Refusals"].append({"reason": '=HYPERLINK("http://evil")', "count": 1})
    wb = load_workbook(io.BytesIO(to_xlsx(sheets)))
    assert set(wb.sheetnames) >= {"Summary", "Refusals", "Spend by model", "Top risk systems"}
    vals = [c.value for c in wb["Refusals"]["A"]]
    assert '\'=HYPERLINK("http://evil")' in vals and wb["Summary"]["A1"].font.bold
