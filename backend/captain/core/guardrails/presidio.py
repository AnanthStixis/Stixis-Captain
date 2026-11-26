"""Microsoft Presidio adapter: NER-based personal-data detection (names, locations,
organisations, dates of birth, …) that regex cannot find.

Presidio is an optional dependency (`pip install -e ".[presidio]"` plus a spaCy
language model). The analyzer is injected, so the Core never hard-depends on it and
tests can use a stand-in. Combine with RegexPiiDetector, which keeps checksum-validated
identifiers (Aadhaar, PAN, cards):

    pii = CombinedDetector(RegexPiiDetector(), PresidioDetector(AnalyzerEngine()))

Limits, stated honestly: NER recall depends on the language model and the language
(English by default); short or informal text lowers accuracy. Thresholds are tuned
on the golden sets per tier, and false-block rates are reported, never assumed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from captain.core.guardrails.detectors import Detector, Span
from captain.core.models import FindingType

# Presidio entity → Captain entity name (kept stable for policies and reports).
# DATE_TIME is deliberately excluded: on its own a date is not personal data, and it
# fires on ordinary words ("quarterly"). Add it per client if dates of birth matter.
DEFAULT_ENTITIES = {
    "PERSON": "PERSON",
    "LOCATION": "LOCATION",
    "NRP": "NATIONALITY_RELIGION_POLITICAL",
    "MEDICAL_LICENSE": "MEDICAL_LICENSE",
    "US_BANK_NUMBER": "BANK_ACCOUNT",
    "IBAN_CODE": "IBAN",
    "US_PASSPORT": "PASSPORT",
    "UK_NHS": "UK_NHS",
}


class PresidioDetector:
    name = "presidio"

    def __init__(
        self,
        analyzer: Any,
        entities: dict[str, str] | None = None,
        score_threshold: float = 0.6,
        language: str = "en",
    ) -> None:
        self._a = analyzer
        self._map = entities or DEFAULT_ENTITIES
        self._threshold = score_threshold
        self._lang = language

    def detect(self, text: str) -> list[Span]:
        results = self._a.analyze(
            text=text,
            entities=list(self._map),
            language=self._lang,
            score_threshold=self._threshold,
        )
        return [
            Span(FindingType.PII, self._map[r.entity_type], r.start, r.end, float(r.score))
            for r in results
            if r.entity_type in self._map
        ]


class CombinedDetector:
    """Runs several PII detectors in priority order. When spans overlap, the earlier
    detector wins: put precise detectors (regex + checksums) before NER, so a validated
    PAN is never relabelled as a LOCATION by a broader NER span."""

    name = "combined:pii"

    def __init__(self, *detectors: Detector) -> None:
        self._ds = detectors

    def detect(self, text: str) -> list[Span]:
        out: list[Span] = []
        for d in self._ds:
            for s in d.detect(text):
                if not any(s.start < o.end and o.start < s.end for o in out):
                    out.append(s)
        return _dedupe(out)


def _dedupe(spans: Iterable[Span]) -> list[Span]:
    seen: set[tuple[str, int, int]] = set()
    out = []
    for s in spans:
        k = (s.entity, s.start, s.end)
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out
