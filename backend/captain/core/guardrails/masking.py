"""Typed, consistent placeholder masking with in-request restoration.

`<EMAIL_ADDRESS_1>` replaces the first distinct e-mail in the request, and the same
value always gets the same placeholder within one request. The mapping lives only in
memory for the lifetime of the request; it is never logged or persisted.
"""

from __future__ import annotations

from collections.abc import Iterable

from captain.core.guardrails.detectors import Span


class MaskMap:
    def __init__(self) -> None:
        self._by_value: dict[str, str] = {}
        self._by_token: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def token_for(self, entity: str, value: str) -> str:
        if value in self._by_value:
            return self._by_value[value]
        n = self._counts.get(entity, 0) + 1
        self._counts[entity] = n
        token = f"<{entity}_{n}>"
        self._by_value[value] = token
        self._by_token[token] = value
        return token

    def restore(self, text: str) -> str:
        for token, value in self._by_token.items():
            text = text.replace(token, value)
        return text

    def __repr__(self) -> str:  # never leak values
        return f"MaskMap(entities={len(self._by_token)})"


def _non_overlapping(spans: Iterable[Span]) -> list[Span]:
    chosen: list[Span] = []
    for s in sorted(spans, key=lambda s: (s.start, -(s.end - s.start))):
        if chosen and s.start < chosen[-1].end:
            continue
        chosen.append(s)
    return chosen


def mask(text: str, spans: Iterable[Span], mapping: MaskMap) -> str:
    out: list[str] = []
    pos = 0
    for s in _non_overlapping(spans):
        out.append(text[pos : s.start])
        out.append(mapping.token_for(s.entity, text[s.start : s.end]))
        pos = s.end
    out.append(text[pos:])
    return "".join(out)
