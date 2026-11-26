"""Output guardrails for streamed responses (CTO F3).

- T1 buffer: hold everything, check, release at the end.
- T2 chunk: release up to the last sentence boundary, keeping a hold-back tail so
  values split across chunks are still caught.
- T3 passthrough_async_check: release immediately; check the full text afterwards
  and log findings.
- T4 passthrough: release immediately; log only.

Secrets in output always stop the stream for T1–T2 (`OutputBlockedError`). Text already
released cannot be recalled; that is why T1 buffers.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from captain.core.decision_api import OutputBlockedError
from captain.core.guardrails.detectors import Detector, RegexPiiDetector, SecretDetector
from captain.core.guardrails.masking import MaskMap, mask

_BOUNDARY = re.compile(r"[.!?\n]\s")


class StreamGuard:
    def __init__(
        self,
        mode: str,
        mapping: MaskMap,
        holdback_chars: int = 64,
        pii: Detector | None = None,
    ) -> None:
        if mode not in ("buffer", "chunk", "passthrough_async_check", "passthrough"):
            raise ValueError(f"unknown streaming mode {mode}")
        self.mode = mode
        self._map = mapping
        self._hold = holdback_chars
        self._pii = pii or RegexPiiDetector()
        self._secrets = SecretDetector()
        self._buf = ""
        self.full_text: list[str] = []  # for async checks (T3/T4); in memory only

    @property
    def enforcing(self) -> bool:
        return self.mode in ("buffer", "chunk")

    def _clean(self, text: str) -> str:
        if secrets := self._secrets.detect(text):
            raise OutputBlockedError(secrets[0].entity)
        # Mask PII the model produced. Restoring the caller's own masked values (when the
        # application needs real values back) is a separate, policy-controlled step.
        return mask(text, self._pii.detect(text), self._map)

    def feed(self, chunk: str) -> Iterator[str]:
        if not self.enforcing:
            self.full_text.append(chunk)
            yield chunk
            return
        self._buf += chunk
        if self.mode == "buffer":
            return
        cut = -1
        limit = len(self._buf) - self._hold
        for m in _BOUNDARY.finditer(self._buf):
            if m.end() <= limit:
                cut = m.end()
        if cut > 0:
            ready, self._buf = self._buf[:cut], self._buf[cut:]
            yield self._clean(ready)

    def finish(self) -> Iterator[str]:
        if self.enforcing and self._buf:
            rest, self._buf = self._buf, ""
            yield self._clean(rest)

    def collected(self) -> str:
        return "".join(self.full_text)
