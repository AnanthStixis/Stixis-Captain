"""Sampling and scoring of governed Tier 1-2 model calls (ADR-005 §2, option (a)).

The judge sees the MASKED prompt and the MASKED answer while they are still in memory
after the response was produced; nothing is written to disk or the database. Judging
runs on a small bounded thread pool so the caller does not wait: if the pool is full the
sample is skipped (counted), never queued without limit. Only scores, the rubric
reference and the judge model reach the evidence ledger, never the content.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from captain.core.alerts import Alert, Severity
from captain.core.evaluation.judge import Judge, JudgeUnavailableError, Rubric
from captain.core.models import CoverageClass, Decision, EventType, UsageEvent

log = logging.getLogger("captain.evaluation")

DEFAULT_SAMPLE_RATE = 0.05


def sampled(request_id: str, rate: float) -> bool:
    """Deterministic per request: the same call is always in or out of the sample."""
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    h = int.from_bytes(hashlib.sha256(request_id.encode()).digest()[:8], "big")
    return h / 2**64 < rate


def conversation_text(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):  # content parts: keep the text ones
            c = "\n".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
        if c:
            lines.append(f"[{m.get('role', '?')}] {c}")
    return "\n".join(lines)


class EvaluationService:
    def __init__(
        self,
        judge: Judge,
        rubric: Rubric,
        enqueue_evidence: Callable[[UsageEvent], object],
        raise_alert: Callable[[Alert], object],
        default_rate: float = DEFAULT_SAMPLE_RATE,
        rates: Mapping[str, float] | None = None,
        workers: int = 2,
        max_pending: int = 16,
    ) -> None:
        self._judge, self._rubric = judge, rubric
        self._enqueue, self._alert = enqueue_evidence, raise_alert
        self._default, self._rates = default_rate, dict(rates or {})
        self._pool = ThreadPoolExecutor(workers, thread_name_prefix="captain-eval")
        self._slots = threading.BoundedSemaphore(max_pending)
        self.skipped = 0

    @property
    def rubric(self) -> Rubric:
        return self._rubric

    def rate_for(self, register_id: str | None) -> float:
        return self._rates.get(register_id or "", self._default)

    def consider(self, d: Decision, messages: list[dict[str, Any]], answer: str) -> bool:
        """Called after a successful response. Returns True when the call was sampled
        and handed to the judge. Never raises: evaluation must not affect the caller."""
        try:
            if not d.risk_tier.fails_closed or not answer:
                return False
            rate = self.rate_for(d.register_id)
            if not sampled(str(d.request_id), rate):
                return False
            if not self._slots.acquire(blocking=False):
                self.skipped += 1
                log.warning("evaluation pool full: sample for %s skipped", d.request_id)
                return False
            conv = conversation_text(messages)
            try:
                fut = self._pool.submit(self._run, d, conv, answer, rate)
            except Exception:
                self._slots.release()
                raise
            fut.add_done_callback(lambda _: self._slots.release())
            return True
        except Exception:
            log.exception("evaluation could not be scheduled for %s", d.request_id)
            return False

    def _event(self, d: Decision, etype: EventType, details: dict[str, Any]) -> UsageEvent:
        return UsageEvent(
            request_id=d.request_id,
            trace_id=d.trace_id,
            event_type=etype,
            channel=d.channel,
            coverage=CoverageClass.GOVERNED,
            principal_id=d.principal_id,
            register_id=d.register_id,
            model=d.model,
            details=details,
        )

    def _run(self, d: Decision, conversation: str, answer: str, rate: float) -> None:
        base: dict[str, Any] = {"rubric": self._rubric.ref, "sample_rate": rate}
        try:
            s = self._judge.score(self._rubric, conversation, answer)
        except JudgeUnavailableError as e:
            self._enqueue(self._event(d, EventType.EVALUATION_FAILED, {**base, "error": str(e)}))
            return
        except Exception:
            log.exception("judge crashed for %s", d.request_id)
            self._enqueue(
                self._event(d, EventType.EVALUATION_FAILED, {**base, "error": "judge crashed"})
            )
            return
        overall = round(s.overall, 3)
        low = overall < self._rubric.alert_below
        scores = {f"score_{k}": v for k, v in s.scores.items()}
        details: dict[str, Any] = {
            **base, "judge_model": s.model, "overall": overall, "low": low, **scores,
        }  # fmt: skip
        self._enqueue(self._event(d, EventType.EVALUATION_SCORED, details))
        if low:
            self._alert(
                Alert(
                    "evaluation.low_score",
                    Severity.WARNING,
                    f"Low evaluation score for {d.register_id}",
                    {
                        "request_id": str(d.request_id),
                        "overall": overall,
                        "threshold": self._rubric.alert_below,
                        "rubric": self._rubric.ref,
                        **scores,
                    },
                    register_id=d.register_id,
                )
            )

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)
