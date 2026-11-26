"""Captain Core latency benchmark on real infrastructure (no test doubles except the
guard model, which is not hosted yet — see NOTE below).

    .venv\\Scripts\\python -m bench.core_latency [--n 500] [--threads 8]

Needs: CAPTAIN_TEST_DATABASE_URL (backend/.env) and tools/opa/opa.exe.
Uses the dedicated test database: migrates it up, and drops the schema at the end.

Measures added latency of Captain's own work (what the CTO's NFR targets):
  A. decide()                              — kill switch, OPA, ledger outbox write
  B. decide() + regex guardrails           — secrets + PII regex on a ~1 KB prompt
  C. decide() + regex + Presidio NER       — the "with guardrails" path
  D. full path: decide + reserve + settle  — adds budget row locks and usage evidence
  E. D under concurrency (threads)
  F. ledger writer throughput              — entries sealed + signed + inserted per second

NOTE: the guard model (prompt-injection classifier) is replaced by a zero-latency
stand-in because no model is hosted yet. Its network + inference time (~30–150 ms on
GPU per the CTO table) must be added to C for Tier 1–3 once it is deployed.
"""

from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from alembic import command  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from captain.core.budget.model import BudgetDefinition, Period  # noqa: E402
from captain.core.context.service import Application, Project  # noqa: E402
from captain.core.factory import BuiltCore, CoreInfra, build_core  # noqa: E402
from captain.core.guardrails.detectors import Detector, RegexPiiDetector  # noqa: E402
from captain.core.guardrails.guard import GuardScore  # noqa: E402
from captain.core.identity.model import PrincipalRecord  # noqa: E402
from captain.core.ledger.chain import Ed25519Signer, KeyRingVerifier  # noqa: E402
from captain.core.ledger.service import InMemoryWormStore  # noqa: E402
from captain.core.models import (  # noqa: E402
    ActionType,
    ActualUsage,
    AutonomyLevel,
    Channel,
    ContentRef,
    ContextHints,
    CostEstimate,
    Credential,
    Environment,
    GovernanceRequest,
    RegisterEntry,
    ResolvedContext,
    RiskTier,
    TokenUsage,
    UsageSource,
)
from captain.core.policy.opa import OpaPolicyEngine  # noqa: E402
from captain.db.bus import PostgresBus  # noqa: E402
from captain.db.stores import (  # noqa: E402
    PgAlertStore,
    PgApprovalStore,
    PgBudgetCatalog,
    PgBudgetStore,
    PgContextStore,
    PgIdentityStore,
    PgKillSwitchStore,
    PgLedgerStore,
    PgRegisterStore,
)
from tests.integration.conftest import URL, _alembic  # noqa: E402
from tests.integration.test_core_real import _free_port, _opa_bin  # noqa: E402

PROMPT = (
    "Summarise this customer complaint for the support lead. The customer, Priya Sharma, "
    "wrote from Bengaluru on behalf of her company and can be reached at priya.sharma@example.com "
    "or +91 98765 43210. She says the invoice for order 4471 was charged twice and wants a refund "
    "to the card on file. Please draft a polite reply that explains next steps, the expected "
    "refund timeline, and who will follow up. Keep it under 150 words and avoid promising dates "
    "we cannot meet. Include a short apology and reference the order number. "
) * 2  # ≈ 1 KB


class ZeroGuard:
    def classify(self, text: str) -> GuardScore:
        return GuardScore(0.01, 0.0, "stand-in")


def start_opa() -> tuple[subprocess.Popen[bytes], str]:
    import httpx

    binary = _opa_bin()
    if not binary:
        raise SystemExit("OPA binary not found (tools/opa/opa.exe)")
    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603
        [binary, "run", "--server", f"--addr=127.0.0.1:{port}", "--log-level=error", "policies"],
        cwd=REPO,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            if httpx.get(f"{url}/health?bundles", timeout=0.5).status_code == 200:
                return proc, url
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    proc.kill()
    raise SystemExit("OPA did not start")


def build(opa_url: str, pii: Detector | None) -> tuple[BuiltCore, ResolvedContext]:
    assert URL
    engine = create_engine(URL, pool_size=30, max_overflow=10)
    signer = Ed25519Signer("bench")
    built = build_core(
        CoreInfra(
            pepper=b"b" * 32,
            policy=OpaPolicyEngine(opa_url, timeout_s=2.0),
            guard=ZeroGuard(),
            signer=signer,
            verifier=KeyRingVerifier({"bench": signer.public_key()}),
            approvers=lambda r: ("fin-1",),
            bus=PostgresBus(URL),
            identity_store=PgIdentityStore(engine),
            context_store=PgContextStore(engine),
            register_store=PgRegisterStore(engine),
            killswitch_store=PgKillSwitchStore(engine),
            budget_catalog=PgBudgetCatalog(engine),
            budget_store=PgBudgetStore(engine),
            approval_store=PgApprovalStore(engine),
            ledger_store=PgLedgerStore(engine),
            worm=InMemoryWormStore(),
            pii=pii,
            alert_store=PgAlertStore(engine),
            notifiers=(),
        )
    )
    ids, cs = built.infra.identity_store, built.infra.context_store
    assert isinstance(cs, PgContextStore)
    ids.put_principal(
        PrincipalRecord(id="bench", type="APPLICATION", org_id="o1", display_name="b", team_id="t1")
    )
    built.register.upsert(
        RegisterEntry(
            id="AI-bench",
            name="bench",
            risk_tier=RiskTier.TIER_3,
            autonomy=AutonomyLevel.A2,
            owner_id="o",
            cost_centre="CC1",
            purpose="benchmark",
            allowed_models=frozenset({"claude-sonnet"}),
        )
    )
    cs.put_project(Project("p1", "o1", "P", "t1"))
    cs.put_application(Application("app-bench", "p1", "AI-bench", "bench"))
    built.infra.budget_catalog.add(BudgetDefinition("org:o1", Period.MONTHLY, 10**15))  # type: ignore[attr-defined]
    raw = built.core.m.identity.issue_key(
        "bench", "AI-bench", "p1", "app-bench", Environment.DEVELOPMENT, None
    ).raw_key
    ctx = built.core.resolve(Credential(kind="virtual_key", secret=raw), ContextHints())
    return built, ctx


def req(ctx: ResolvedContext, with_text: bool) -> GovernanceRequest:
    return GovernanceRequest(
        trace_id="bench",
        context=ctx,
        channel=Channel.API,
        action=ActionType.MODEL_CALL,
        model="claude-sonnet",
        max_output_tokens=500,
        contents=(ContentRef(text=PROMPT),) if with_text else (),
    )


EST = CostEstimate(input_tokens=300, max_output_tokens=500, amount=9_000, price_version="p")
ACT = ActualUsage(
    usage=TokenUsage(input_tokens=300, output_tokens=120),
    source=UsageSource.PROVIDER_REPORTED,
    cost=2_700,
    price_version="p",
    provider="anthropic",
)


def timed(fn, n: int) -> list[float]:  # type: ignore[no-untyped-def]
    for _ in range(min(30, n)):  # warm-up: connections, caches, JIT-free but lazy imports
        fn()
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000)
    return out


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def row(name: str, xs: list[float], target: float | None) -> str:
    p95 = pct(xs, 95)
    verdict = "" if target is None else ("PASS" if p95 <= target else "FAIL")
    tgt = "" if target is None else f"<= {target:.0f}"
    med, p99 = statistics.median(xs), pct(xs, 99)
    return f"| {name} | {med:.1f} | {p95:.1f} | {p99:.1f} | {tgt} | {verdict} |"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--no-presidio", action="store_true")
    a = ap.parse_args()
    if not URL:
        raise SystemExit("CAPTAIN_TEST_DATABASE_URL not set (backend/.env)")
    cfg = _alembic(URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    opa, url = start_opa()
    rows: list[str] = []
    try:
        core_regex, ctx = build(url, RegexPiiDetector())
        c = core_regex.core

        def full() -> None:
            d = c.decide(req(ctx, True))
            c.settle(c.reserve(d, EST), ACT)

        rows.append(
            row("A. decide (no content)", timed(lambda: c.decide(req(ctx, False)), a.n), 50)
        )
        rows.append(
            row(
                "B. decide + regex guardrails (1 KB)",
                timed(lambda: c.decide(req(ctx, True)), a.n),
                50,
            )
        )
        if not a.no_presidio:
            from presidio_analyzer import AnalyzerEngine

            from captain.core.guardrails.presidio import CombinedDetector, PresidioDetector

            pii = CombinedDetector(RegexPiiDetector(), PresidioDetector(AnalyzerEngine()))
            c.m.guardrails._pii = pii  # noqa: SLF001 — same core, NER switched on
            rows.append(
                row(
                    "C. decide + regex + Presidio (1 KB)",
                    timed(lambda: c.decide(req(ctx, True)), a.n),
                    250,
                )
            )
            rows.append(
                row("D. full path + Presidio (decide+reserve+settle)", timed(full, a.n), 250)
            )
            c.m.guardrails._pii = RegexPiiDetector()  # noqa: SLF001
        rows.append(row("D'. full path, regex only", timed(full, a.n), None))

        lat: list[float] = []

        def one(_: int) -> None:
            t = time.perf_counter()
            full()
            lat.append((time.perf_counter() - t) * 1000)

        t0 = time.perf_counter()
        with ThreadPoolExecutor(a.threads) as p:
            list(p.map(one, range(a.n)))
        wall = time.perf_counter() - t0
        rows.append(row(f"E. full path, regex, {a.threads} threads", lat, None))

        pending = len(c.m.ledger._store.outbox_take(10**7))  # noqa: SLF001
        t0 = time.perf_counter()
        written = 0
        while n := c.m.ledger.write_pending(1000):
            written += n
        ledger_s = time.perf_counter() - t0
        ok = c.m.ledger.verify().ok
    finally:
        opa.terminate()
        opa.wait(timeout=10)
        command.downgrade(cfg, "base")

    print(
        f"\nCaptain Core latency - n={a.n} per scenario, {os.cpu_count()} CPUs, "
        "OPA 1.0.0 + PostgreSQL 18 (local)\n"
    )
    print("| Scenario | p50 ms | p95 ms | p99 ms | Target p95 | Result |")
    print("|---|---|---|---|---|---|")
    print("\n".join(rows))
    print(f"\nE throughput: {a.n / wall:.0f} governed calls/s with {a.threads} threads")
    print(
        f"F ledger writer: {written} of {pending} queued entries in {ledger_s:.2f}s = "
        f"{written / ledger_s:.0f} entries/s; verify after: {'ok' if ok else 'FAILED'}"
    )


if __name__ == "__main__":
    main()
