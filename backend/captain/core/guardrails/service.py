"""Guardrail pipeline by tier (CTO table, Problem 3).

| Stage              | T1    | T2    | T3   | T4  |
|--------------------|-------|-------|------|-----|
| Deterministic      | run   | run   | run  | run |
| Personal data      | run   | run   | run  | run |
| Guard model        | run   | run   | run  | off |

Detection only: this module reports findings. The OPA policy decides block / mask /
log from the findings, and `apply_redaction` performs masking when told to.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from captain.core.guardrails.detectors import (
    BannedTermDetector,
    Detector,
    RegexPiiDetector,
    SecretDetector,
    Span,
    to_findings,
)
from captain.core.guardrails.guard import GuardModel, GuardUnavailableError
from captain.core.guardrails.masking import MaskMap, mask
from captain.core.models import (
    ContentRef,
    FindingType,
    GuardrailFinding,
    RiskTier,
)

GUARD_TIERS = {RiskTier.TIER_1, RiskTier.TIER_2, RiskTier.TIER_3}


@dataclass
class GuardrailReport:
    findings: list[GuardrailFinding] = field(default_factory=list)
    guard_unavailable: bool = False
    oversize: bool = False


class GuardrailService:
    def __init__(
        self,
        guard: GuardModel | None,
        pii: Detector | None = None,
        banned_terms: Sequence[str] = (),
        max_content_chars: int = 400_000,
    ) -> None:
        self._guard = guard
        self._secrets: Detector = SecretDetector()
        self._pii: Detector = pii or RegexPiiDetector()
        self._banned: Detector = BannedTermDetector(banned_terms)
        self._max = max_content_chars

    @property
    def pii_detector(self) -> Detector:
        return self._pii

    def inspect(self, contents: Sequence[ContentRef], tier: RiskTier) -> GuardrailReport:
        report = GuardrailReport()
        for i, c in enumerate(contents):
            if c.text is None:  # hash-only content cannot be inspected; policy sees no findings
                continue
            if len(c.text) > self._max:
                report.oversize = True
                report.findings.append(
                    GuardrailFinding(
                        type=FindingType.SIZE_LIMIT,
                        entity="CONTENT_TOO_LARGE",
                        detector="size",
                        content_index=i,
                    )  # fmt: skip
                )
                continue
            for d in (self._secrets, self._pii, self._banned):
                report.findings += to_findings(d.detect(c.text), d.name, i)
            if tier in GUARD_TIERS:
                self._run_guard(c, i, report)
        return report

    def _run_guard(self, c: ContentRef, i: int, report: GuardrailReport) -> None:
        if self._guard is None:
            report.guard_unavailable = True
            return
        try:
            s = self._guard.classify(c.text or "")
        except GuardUnavailableError:
            report.guard_unavailable = True
            return
        # Untrusted content (retrieved docs, tool results, web pages) is the main
        # injection vector; report its score as-is. Policy thresholds decide.
        report.findings.append(
            GuardrailFinding(
                type=FindingType.INJECTION,
                entity="prompt_injection" if c.trusted else "indirect_prompt_injection",
                detector=f"guard:{s.model}",
                content_index=i,
                score=s.injection,
            )  # fmt: skip
        )
        if s.unsafe > 0:
            report.findings.append(
                GuardrailFinding(
                    type=FindingType.UNSAFE,
                    entity="unsafe_content",
                    detector=f"guard:{s.model}",
                    content_index=i,
                    score=s.unsafe,
                )  # fmt: skip
            )


def apply_redaction(
    contents: Sequence[ContentRef], findings: Sequence[GuardrailFinding], mapping: MaskMap
) -> tuple[ContentRef, ...]:
    """Mask PII and secrets in place, using the findings' character offsets."""
    out: list[ContentRef] = []
    for i, c in enumerate(contents):
        if c.text is None:
            out.append(c)
            continue
        spans = [
            Span(f.type, f.entity, f.start, f.end)
            for f in findings
            if f.content_index == i
            and f.type in (FindingType.PII, FindingType.SECRET)
            and f.start is not None
            and f.end is not None
        ]
        out.append(c.model_copy(update={"text": mask(c.text, spans, mapping)}))
    return tuple(out)
