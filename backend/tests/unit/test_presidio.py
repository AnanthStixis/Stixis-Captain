"""Presidio adapter with a stand-in analyzer that follows Presidio's analyze() shape
(entity_type, start, end, score). A real-analyzer test runs if presidio is installed."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from captain.core.guardrails.detectors import RegexPiiDetector
from captain.core.guardrails.masking import MaskMap, mask
from captain.core.guardrails.presidio import CombinedDetector, PresidioDetector
from captain.core.guardrails.service import GuardrailService
from captain.core.models import ContentRef, RiskTier


class FakeAnalyzer:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def analyze(self, **kw: Any) -> list[SimpleNamespace]:
        self.kwargs = kw
        t = kw["text"]
        out = []
        if "Priya Sharma" in t:
            i = t.index("Priya Sharma")
            out.append(SimpleNamespace(entity_type="PERSON", start=i, end=i + 12, score=0.85))
        out.append(SimpleNamespace(entity_type="URL", start=0, end=1, score=0.9))  # not mapped
        return out


def test_maps_entities_and_ignores_unmapped() -> None:
    a = FakeAnalyzer()
    spans = PresidioDetector(a, score_threshold=0.7).detect("Ask Priya Sharma")
    assert [(s.entity, s.start, s.end) for s in spans] == [("PERSON", 4, 16)]
    assert a.kwargs["score_threshold"] == 0.7 and a.kwargs["language"] == "en"


def test_combined_with_regex_masks_both() -> None:
    det = CombinedDetector(RegexPiiDetector(), PresidioDetector(FakeAnalyzer()))
    text = "Priya Sharma, priya@corp.com"
    assert mask(text, det.detect(text), MaskMap()) == "<PERSON_1>, <EMAIL_ADDRESS_1>"


def test_pipeline_uses_combined_detector() -> None:
    svc = GuardrailService(
        None, pii=CombinedDetector(RegexPiiDetector(), PresidioDetector(FakeAnalyzer()))
    )
    rep = svc.inspect([ContentRef(text="Priya Sharma wrote")], RiskTier.TIER_4)
    assert {f.entity for f in rep.findings} == {"PERSON"}
    assert all(f.detector == "combined:pii" for f in rep.findings)


def test_real_presidio_if_installed() -> None:
    analyzer_mod = pytest.importorskip("presidio_analyzer")
    try:
        engine = analyzer_mod.AnalyzerEngine()
    except Exception as e:  # spaCy model missing
        pytest.skip(f"Presidio installed but not usable: {e}")
    spans = PresidioDetector(engine, score_threshold=0.5).detect(
        "My name is John Smith and I live in London."
    )
    assert {"PERSON", "LOCATION"} <= {s.entity for s in spans}


def test_precise_detector_wins_overlap() -> None:
    class BroadNer:
        def analyze(self, **kw: Any) -> list[SimpleNamespace]:
            return [SimpleNamespace(entity_type="LOCATION", start=0, end=14, score=0.9)]

    det = CombinedDetector(RegexPiiDetector(), PresidioDetector(BroadNer()))
    spans = det.detect("PAN ABCPE1234F")
    assert [s.entity for s in spans] == ["IN_PAN"]


def test_real_presidio_quality_samples() -> None:
    """Regression for issues seen with en_core_web_lg: PAN relabelled as LOCATION,
    'quarterly' flagged as DATE_TIME."""
    analyzer_mod = pytest.importorskip("presidio_analyzer")
    try:
        engine = analyzer_mod.AnalyzerEngine()
    except Exception as e:
        pytest.skip(f"Presidio installed but not usable: {e}")
    det = CombinedDetector(RegexPiiDetector(), PresidioDetector(engine))
    s = "Rahul Verma, PAN ABCPE1234F, called from Bengaluru."
    assert (
        mask(s, det.detect(s), MaskMap()) == "<PERSON_1>, PAN <IN_PAN_1>, called from <LOCATION_1>."
    )
    assert det.detect("Summarise the quarterly revenue report for the board.") == []
