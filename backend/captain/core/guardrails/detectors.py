"""Deterministic detectors: secrets, personal data (regex + checksums), banned terms, size.

These are fast (< 5 ms) and run on every request. They are not perfect:
- Regex PII detection misses names, addresses and free-form identifiers (Presidio's
  NER covers those; see presidio adapter, Phase 1 week 2).
- Checksums (Luhn, Verhoeff) cut false positives for cards and Aadhaar but do not
  prove a number is real.
Detection rates are measured on the golden sets, never assumed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from captain.core.models import FindingType, GuardrailFinding


@dataclass(frozen=True, slots=True)
class Span:
    type: FindingType
    entity: str
    start: int
    end: int
    score: float = 1.0


class Detector(Protocol):
    name: str

    def detect(self, text: str) -> list[Span]: ...


# --------------------------------------------------------------- checksums


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


_V_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]  # fmt: skip
_V_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]  # fmt: skip


def verhoeff_ok(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _V_D[c][_V_P[i % 8][int(ch)]]
    return c == 0


# --------------------------------------------------------------- detectors


@dataclass(frozen=True)
class _Rule:
    entity: str
    pattern: re.Pattern[str]
    check: str | None = None  # "luhn" | "verhoeff"


def _scan(text: str, rules: Iterable[_Rule], ftype: FindingType) -> list[Span]:
    out: list[Span] = []
    for r in rules:
        for m in r.pattern.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if r.check == "luhn" and not luhn_ok(digits):
                continue
            if r.check == "verhoeff" and not verhoeff_ok(digits):
                continue
            out.append(Span(ftype, r.entity, m.start(), m.end()))
    return out


class SecretDetector:
    name = "regex:secrets"
    RULES = (
        _Rule("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
        _Rule("PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")),
        _Rule("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
        _Rule("OPENAI_KEY", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
        _Rule("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
        _Rule(
            "CAPTAIN_KEY",
            re.compile(r"\bcpt_(?:live|test)_[0-9a-f]{16}_[0-9a-f]{48}_[0-9a-f]{8}\b"),
        ),
        _Rule(
            "JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
        ),
        _Rule("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    )

    def detect(self, text: str) -> list[Span]:
        return _scan(text, self.RULES, FindingType.SECRET)


class RegexPiiDetector:
    name = "regex:pii"
    RULES = (
        _Rule("EMAIL_ADDRESS", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
        _Rule("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), check="luhn"),
        _Rule("IN_AADHAAR", re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b"), check="verhoeff"),
        _Rule("IN_PAN", re.compile(r"\b[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z]\b")),
        _Rule("US_SSN", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
        _Rule(
            "PHONE_NUMBER",
            re.compile(r"(?<![\w+])\+\d{1,3}[ -]?\(?\d{2,4}\)?(?:[ -]?\d{2,4}){2,3}\b"),
        ),
        _Rule(
            "IP_ADDRESS",
            re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
        ),
    )

    def detect(self, text: str) -> list[Span]:
        spans = _scan(text, self.RULES, FindingType.PII)
        # Aadhaar numbers are 12 digits and can also pass Luhn; keep the more specific label.
        aadhaar = {(s.start, s.end) for s in spans if s.entity == "IN_AADHAAR"}
        return [s for s in spans if not (s.entity == "CREDIT_CARD" and (s.start, s.end) in aadhaar)]


class BannedTermDetector:
    name = "terms:banned"

    def __init__(self, terms: Iterable[str]) -> None:
        cleaned = [re.escape(t.strip()) for t in terms if t.strip()]
        self._re = re.compile(r"\b(?:" + "|".join(cleaned) + r")\b", re.I) if cleaned else None

    def detect(self, text: str) -> list[Span]:
        if self._re is None:
            return []
        return [
            Span(FindingType.BANNED_TERM, "BANNED_TERM", m.start(), m.end())
            for m in self._re.finditer(text)
        ]


def to_findings(spans: Iterable[Span], detector: str, content_index: int) -> list[GuardrailFinding]:
    return [
        GuardrailFinding(
            type=s.type,
            entity=s.entity,
            detector=detector,
            content_index=content_index,
            start=s.start,
            end=s.end,
            score=s.score,
        )  # fmt: skip
        for s in spans
    ]
