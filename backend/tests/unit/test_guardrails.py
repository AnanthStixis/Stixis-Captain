from __future__ import annotations

import httpx
import pytest

from captain.core.guardrails.detectors import (
    RegexPiiDetector,
    SecretDetector,
    luhn_ok,
    verhoeff_ok,
)
from captain.core.guardrails.guard import GuardScore, GuardUnavailableError, HttpGuardClient
from captain.core.guardrails.masking import MaskMap, mask
from captain.core.guardrails.service import GuardrailService, apply_redaction
from captain.core.guardrails.streaming import OutputBlockedError, StreamGuard
from captain.core.models import ContentRef, FindingType, RiskTier

# Test fixtures only: well-known checksum-valid test numbers, not real identifiers.
VISA_TEST = "4111 1111 1111 1111"
AADHAAR_TEST = "2341 2341 2346"  # Verhoeff-valid synthetic value


class FixedGuard:
    def __init__(self, injection: float = 0.0, fail: bool = False) -> None:
        self.injection, self.fail, self.calls = injection, fail, 0

    def classify(self, text: str) -> GuardScore:
        self.calls += 1
        if self.fail:
            raise GuardUnavailableError("down")
        return GuardScore(self.injection, 0.0, "fixed")


def entities(text: str) -> set[str]:
    return {s.entity for s in RegexPiiDetector().detect(text)}


def test_checksums() -> None:
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")
    assert verhoeff_ok(AADHAAR_TEST.replace(" ", ""))
    assert not verhoeff_ok("234123412347")


def test_pii_detection() -> None:
    assert entities("mail a.b@example.com now") == {"EMAIL_ADDRESS"}
    assert entities(f"card {VISA_TEST}") == {"CREDIT_CARD"}
    assert entities("card 4111 1111 1111 1112") == set()  # fails Luhn
    assert entities(f"aadhaar {AADHAAR_TEST}") == {"IN_AADHAAR"}
    assert entities("pan ABCPE1234F") == {"IN_PAN"}
    assert entities("call +91 98765 43210") == {"PHONE_NUMBER"}
    assert entities("nothing to see") == set()


def test_secret_detection() -> None:
    hits = {
        s.entity for s in SecretDetector().detect("key AKIAABCDEFGHIJKLMNOP and ghp_" + "a" * 36)
    }
    assert hits == {"AWS_ACCESS_KEY", "GITHUB_TOKEN"}


def test_mask_consistent_and_restorable() -> None:
    text = "a@x.com wrote to b@y.org and a@x.com"
    m = MaskMap()
    masked = mask(text, RegexPiiDetector().detect(text), m)
    assert masked == "<EMAIL_ADDRESS_1> wrote to <EMAIL_ADDRESS_2> and <EMAIL_ADDRESS_1>"
    assert m.restore(masked) == text
    assert "a@x.com" not in repr(m)


def test_pipeline_tiers_and_guard_skipped_for_tier4() -> None:
    g = FixedGuard(injection=0.9)
    svc = GuardrailService(g)
    rep = svc.inspect([ContentRef(text="hi a@x.com")], RiskTier.TIER_2)
    types = {f.type for f in rep.findings}
    assert types == {FindingType.PII, FindingType.INJECTION} and g.calls == 1
    svc.inspect([ContentRef(text="hi")], RiskTier.TIER_4)
    assert g.calls == 1


def test_guard_down_is_reported_not_hidden() -> None:
    rep = GuardrailService(FixedGuard(fail=True)).inspect([ContentRef(text="x")], RiskTier.TIER_1)
    assert rep.guard_unavailable


def test_findings_never_contain_text() -> None:
    rep = GuardrailService(FixedGuard()).inspect([ContentRef(text="me@x.com")], RiskTier.TIER_3)
    assert "me@x.com" not in repr(rep.findings)


def test_hash_only_content_is_skipped() -> None:
    rep = GuardrailService(FixedGuard()).inspect([ContentRef(content_hmac="h")], RiskTier.TIER_1)
    assert rep.findings == []


def test_oversize() -> None:
    rep = GuardrailService(None, max_content_chars=5).inspect(
        [ContentRef(text="123456")], RiskTier.TIER_4
    )
    assert rep.oversize


def test_apply_redaction() -> None:
    c = [ContentRef(text="mail me@x.com")]
    rep = GuardrailService(None).inspect(c, RiskTier.TIER_4)
    red = apply_redaction(c, rep.findings, MaskMap())
    assert red[0].text == "mail <EMAIL_ADDRESS_1>"


def test_http_guard_failover_and_cache() -> None:
    hits: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        hits.append(req.url.host)
        if req.url.host == "g1":
            return httpx.Response(503)
        return httpx.Response(200, json={"injection": 0.8, "unsafe": 0.0, "model": "gg"})

    c = HttpGuardClient(["http://g1", "http://g2"], transport=httpx.MockTransport(h))
    assert c.classify("t").injection == 0.8
    c.classify("t")
    assert hits == ["g1", "g2"]  # second call served from cache


def test_http_guard_all_down_and_bad_scores() -> None:
    c = HttpGuardClient(
        ["http://g1"],
        fallback="http://fb",
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    with pytest.raises(GuardUnavailableError):
        c.classify("t")
    bad = HttpGuardClient(["http://g"], transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"injection": 7, "unsafe": 0})))  # fmt: skip
    with pytest.raises(GuardUnavailableError):
        bad.classify("t")


# ------------------------------------------------------------------ streaming


def run(sg: StreamGuard, chunks: list[str]) -> str:
    out = [piece for c in chunks for piece in sg.feed(c)]
    out += list(sg.finish())
    return "".join(out)


def test_buffer_mode_emits_nothing_until_finish() -> None:
    sg = StreamGuard("buffer", MaskMap())
    assert list(sg.feed("Hello there. ")) == []
    assert "".join(sg.finish()) == "Hello there. "


def test_chunk_mode_catches_value_split_across_chunks() -> None:
    sg = StreamGuard("chunk", MaskMap(), holdback_chars=16)
    text = run(sg, ["Sure. Contact jo", "hn@exam", "ple.com for help. Thanks. "])
    assert "john@example.com" not in text and "<EMAIL_ADDRESS_1>" in text


def test_chunk_mode_streams_progressively() -> None:
    sg = StreamGuard("chunk", MaskMap(), holdback_chars=4)
    first = list(sg.feed("One sentence here. Another sentence follows. "))
    assert first and first[0].startswith("One sentence here.")


def test_secret_in_output_stops_stream() -> None:
    with pytest.raises(OutputBlockedError):
        run(StreamGuard("buffer", MaskMap()), ["leaked AKIAABCDEFGHIJKLMNOP"])


def test_passthrough_modes_do_not_alter() -> None:
    sg = StreamGuard("passthrough_async_check", MaskMap())
    assert run(sg, ["a@x.com", " ok"]) == "a@x.com ok"
    assert sg.collected() == "a@x.com ok"
