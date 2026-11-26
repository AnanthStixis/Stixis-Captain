"""Live evaluation (ADR-005 §2): sampling, the judge client, evidence and alerts, and the
gateway hook. The judge is a test double or an httpx mock: no model is called."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from captain.config import DEFAULT_RUBRIC, ConfigError, load_settings
from captain.core.alerts import Alert
from captain.core.evaluation.judge import (
    HttpJudgeClient,
    JudgeScore,
    JudgeUnavailableError,
    Rubric,
    parse_scores,
)
from captain.core.evaluation.service import EvaluationService, conversation_text, sampled
from captain.core.models import (
    ActionType,
    AutonomyLevel,
    Channel,
    ContentRef,
    Decision,
    GovernanceRequest,
    PrincipalType,
    RiskTier,
    UsageEvent,
)
from tests.conftest import make_context
from tests.harness.adapters import CaptainCoreHarness
from tests.unit.test_engine import RedactPolicy
from tests.unit.test_gateway import chat, gateway, ledger_types

RUBRIC = Rubric("t", "1", {"grounded": "g", "policy": "p", "safety": "s"}, alert_below=0.6)


class FakeJudge:
    def __init__(self, scores: dict[str, float] | None = None, fail: bool = False) -> None:
        self.scores = scores or {"grounded": 0.9, "policy": 0.95, "safety": 1.0}
        self.fail = fail
        self.seen: list[tuple[str, str]] = []
        self.release = threading.Event()
        self.release.set()

    def score(self, rubric: Rubric, conversation: str, answer: str) -> JudgeScore:
        self.release.wait(5)
        self.seen.append((conversation, answer))
        if self.fail:
            raise JudgeUnavailableError("judge failed: ConnectError")
        return JudgeScore(dict(self.scores), "judge-x")


def service(
    judge: FakeJudge, rate: float = 1.0, **kw: Any
) -> tuple[EvaluationService, list[UsageEvent], list[Alert]]:
    events: list[UsageEvent] = []
    alerts: list[Alert] = []
    svc = EvaluationService(judge, RUBRIC, events.append, alerts.append, rate, **kw)
    return svc, events, alerts


def decision(tier: RiskTier = RiskTier.TIER_2) -> Decision:
    ctx = make_context("app", tier, AutonomyLevel.A2, PrincipalType.APPLICATION)
    req = GovernanceRequest(
        trace_id="t-1",
        context=ctx,
        channel=Channel.API,
        action=ActionType.MODEL_CALL,
        model="claude-sonnet",
        contents=(ContentRef(text="hi", trusted=False),),
    )
    return CaptainCoreHarness().core.decide(req)


# ------------------------------------------------------------------- sampling


def test_sampling_is_deterministic_and_close_to_the_rate() -> None:
    ids = [f"req-{i}" for i in range(20_000)]
    hits = [i for i in ids if sampled(i, 0.05)]
    assert 0.04 < len(hits) / len(ids) < 0.06
    assert hits == [i for i in ids if sampled(i, 0.05)]  # same calls every time
    assert not any(sampled(i, 0) for i in ids[:100])
    assert all(sampled(i, 1) for i in ids[:100])


def test_conversation_text_keeps_text_parts_only() -> None:
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image_url"}]},
        {"role": "assistant", "content": None},
    ]
    assert conversation_text(msgs) == "[system] be brief\n[user] a\n"


# ------------------------------------------------------------------- rubric/judge


def test_repository_rubric_loads() -> None:
    r = Rubric.load(DEFAULT_RUBRIC)
    assert set(r.criteria) == {"grounded", "policy", "safety"}
    assert r.alert_below == 0.6


def test_rubric_rejects_bad_threshold(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"id": "x", "version": "1", "criteria": {"a": "b"}, "alert_below": 2}))
    with pytest.raises(ValueError, match="alert_below"):
        Rubric.load(p)


@pytest.mark.parametrize(
    "reply",
    [
        "no json here",
        '{"grounded": 0.9, "policy": 0.9}',
        '{"grounded": 1.5, "policy": 1, "safety": 1}',
    ],
)
def test_parse_scores_rejects_incomplete_or_out_of_range(reply: str) -> None:
    with pytest.raises((ValueError, KeyError)):
        parse_scores(reply, RUBRIC)


def test_http_judge_sends_rubric_and_data_and_parses_reply() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        reply = 'Scores: {"grounded": 0.8, "policy": 1, "safety": 0.4}'
        return httpx.Response(
            200, json={"model": "judge-v2", "choices": [{"message": {"content": reply}}]}
        )

    j = HttpJudgeClient("http://judge.local/", "j", "k1", transport=httpx.MockTransport(handler))
    s = j.score(RUBRIC, "[user] <EMAIL_ADDRESS_1>", "ok")
    assert seen["url"] == "http://judge.local/v1/chat/completions"
    assert seen["auth"] == "Bearer k1"
    assert seen["body"]["temperature"] == 0
    system, user = seen["body"]["messages"]
    assert "never instructions" in system["content"] and "- safety: s" in system["content"]
    assert "<EMAIL_ADDRESS_1>" in user["content"] and "<answer>\nok\n</answer>" in user["content"]
    assert s.scores == {"grounded": 0.8, "policy": 1.0, "safety": 0.4}
    assert s.overall == 0.4 and s.model == "judge-v2"


def test_http_judge_errors_become_judge_unavailable() -> None:
    j = HttpJudgeClient(
        "http://judge.local", "j", transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    with pytest.raises(JudgeUnavailableError):
        j.score(RUBRIC, "c", "a")


# ------------------------------------------------------------------- service


def test_good_score_records_evidence_without_content_and_no_alert() -> None:
    svc, events, alerts = service(FakeJudge())
    d = decision()
    assert svc.consider(d, [{"role": "user", "content": "secret prompt"}], "secret answer")
    svc.shutdown()
    [e] = events
    assert e.event_type.value == "evaluation.scored"
    assert e.request_id == d.request_id and e.register_id == d.register_id
    assert e.details is not None
    assert e.details["overall"] == 0.9 and e.details["low"] is False
    assert e.details["rubric"] == "t@1" and e.details["judge_model"] == "judge-x"
    assert "secret" not in e.model_dump_json()
    assert alerts == []


def test_low_score_raises_alert() -> None:
    svc, events, alerts = service(FakeJudge({"grounded": 0.2, "policy": 0.9, "safety": 1.0}))
    svc.consider(decision(), [{"role": "user", "content": "q"}], "a")
    svc.shutdown()
    assert events[0].details is not None and events[0].details["low"] is True
    [a] = alerts
    assert a.kind == "evaluation.low_score" and a.details["overall"] == 0.2


def test_judge_failure_is_recorded_not_raised() -> None:
    svc, events, alerts = service(FakeJudge(fail=True))
    assert svc.consider(decision(), [{"role": "user", "content": "q"}], "a")
    svc.shutdown()
    [e] = events
    assert e.event_type.value == "evaluation.failed" and alerts == []


@pytest.mark.parametrize("tier", [RiskTier.TIER_3, RiskTier.TIER_4])
def test_tier_3_4_are_never_judged(tier: RiskTier) -> None:
    judge = FakeJudge()
    svc, events, _ = service(judge)
    assert not svc.consider(decision(tier), [{"role": "user", "content": "q"}], "a")
    svc.shutdown()
    assert judge.seen == [] and events == []


def test_per_entry_rate_overrides_default() -> None:
    d = decision()
    svc, _, _ = service(FakeJudge(), rate=1.0, rates={d.register_id: 0.0})
    assert not svc.consider(d, [{"role": "user", "content": "q"}], "a")
    assert svc.rate_for("other") == 1.0


def test_full_pool_skips_instead_of_queueing() -> None:
    judge = FakeJudge()
    judge.release.clear()  # the judge hangs until released
    svc, events, _ = service(judge, workers=1, max_pending=2)
    msgs = [{"role": "user", "content": "q"}]
    assert svc.consider(decision(), msgs, "a")
    assert svc.consider(decision(), msgs, "a")
    assert not svc.consider(decision(), msgs, "a")  # over the bound: skipped
    assert svc.skipped == 1
    judge.release.set()
    svc.shutdown()
    assert len(events) == 2


# ------------------------------------------------------------------- config


def test_judge_url_needs_model_and_rates_are_bounded() -> None:
    base = {
        "CAPTAIN_DATABASE_URL": "postgresql+psycopg://x",
        "CAPTAIN_KEY_PEPPER": "00" * 32,
        "CAPTAIN_LEDGER_KEY": "pem:k.pem",
        "CAPTAIN_WORM": "dir:w",
    }
    s = load_settings(base)
    assert s.judge_url is None and s.eval_sample_rate == 0.05
    with pytest.raises(ConfigError, match="JUDGE_MODEL"):
        load_settings({**base, "CAPTAIN_JUDGE_URL": "http://judge"})
    with pytest.raises(ConfigError, match="between 0 and 1"):
        load_settings({**base, "CAPTAIN_EVAL_RATES": '{"reg": 3}'})
    s = load_settings(
        {**base, "CAPTAIN_JUDGE_URL": "http://judge", "CAPTAIN_JUDGE_MODEL": "m",
         "CAPTAIN_EVAL_RATES": '{"reg": 0.5}'}
    )  # fmt: skip
    assert s.judge_model == "m" and s.eval_rates == {"reg": 0.5}


# ------------------------------------------------------------------- gateway hook


def _gateway_with_judge(stream: bool) -> tuple[Any, FakeJudge, list[Alert]]:
    h, c, hdr = gateway(tier=RiskTier.TIER_2, policy=RedactPolicy())
    judge = FakeJudge()
    alerts: list[Alert] = []
    svc = EvaluationService(judge, RUBRIC, h.core.m.ledger.enqueue, alerts.append, 1.0)
    h.core.m.evaluation = svc
    r = chat(c, hdr, text="email ravi@example.com", stream=stream)
    assert r.status_code == 200
    svc.shutdown()
    return h, judge, alerts


@pytest.mark.parametrize("stream", [False, True])
def test_gateway_offers_masked_text_to_the_judge(stream: bool) -> None:
    h, judge, _ = _gateway_with_judge(stream)
    [(conv, answer)] = judge.seen
    assert "ravi@example.com" not in conv and "<EMAIL_ADDRESS_1>" in conv
    assert "ravi@example.com" not in answer and answer
    scored = [b for t, b in ledger_types(h) if t == "evaluation.scored"]
    assert len(scored) == 1
    assert "ravi" not in json.dumps(scored[0])


def test_gateway_without_evaluation_is_unchanged() -> None:
    h, c, hdr = gateway(tier=RiskTier.TIER_2)
    assert chat(c, hdr).status_code == 200
    assert not [t for t, _ in ledger_types(h) if t.startswith("evaluation.")]
