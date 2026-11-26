"""Normalize provider usage payloads into Captain's TokenUsage.

Only documented fields are read; anything missing becomes 0 and the caller marks the
usage source accordingly. Unknown providers return None so the caller records
UsageSource.ESTIMATED or UNAVAILABLE instead of inventing numbers.
"""

from __future__ import annotations

from collections.abc import Mapping

from captain.core.models import TokenUsage


def _int(v: object) -> int:
    return v if isinstance(v, int) and v >= 0 else 0


def _sub(d: Mapping[str, object], key: str) -> Mapping[str, object]:
    v = d.get(key)
    return v if isinstance(v, Mapping) else {}


def from_openai(usage: Mapping[str, object]) -> TokenUsage:
    """OpenAI Chat Completions `usage`: prompt_tokens, completion_tokens,
    prompt_tokens_details.cached_tokens, completion_tokens_details.reasoning_tokens.
    Reasoning tokens are already included in completion_tokens."""
    return TokenUsage(
        input_tokens=_int(usage.get("prompt_tokens")),
        output_tokens=_int(usage.get("completion_tokens")),
        cached_input_tokens=_int(_sub(usage, "prompt_tokens_details").get("cached_tokens")),
        reasoning_tokens=_int(_sub(usage, "completion_tokens_details").get("reasoning_tokens")),
    )


def from_anthropic(usage: Mapping[str, object]) -> tuple[TokenUsage, int]:
    """Anthropic Messages `usage`: input_tokens excludes cache reads/writes, which are
    reported as cache_read_input_tokens / cache_creation_input_tokens.
    Returns (usage, cache_write_tokens)."""
    cache_read = _int(usage.get("cache_read_input_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    return (
        TokenUsage(
            input_tokens=_int(usage.get("input_tokens")) + cache_read,
            output_tokens=_int(usage.get("output_tokens")),
            cached_input_tokens=cache_read,
        ),
        cache_write,
    )


def normalize(provider: str, usage: Mapping[str, object] | None) -> tuple[TokenUsage, int] | None:
    if not usage:
        return None
    match provider:
        case "openai" | "azure" | "litellm":
            return from_openai(usage), 0
        case "anthropic":
            return from_anthropic(usage)
        case _:
            return None
