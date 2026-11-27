"""Suggested list prices (US$ per 1M tokens, input / output) for common models, used to
pre-fill the price when an admin adds models straight from their AI provider.

Suggestions only: the admin sees and can change them before saving, and the provider's
pricing page is the authority. Matched by the longest known prefix of the model id, so
dated versions (gpt-4o-mini-2024-07-18) find their family. Unknown models get no
suggestion and the admin enters the price."""

from __future__ import annotations

LIST_PRICES: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "o4-mini": (1.10, 4.40),
    "o3-mini": (1.10, 4.40),
    "o3": (2.00, 8.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    # Anthropic
    "claude-haiku": (1.00, 5.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-haiku": (0.25, 1.25),
    "claude-sonnet": (3.00, 15.00),
    "claude-3-7-sonnet": (3.00, 15.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus": (15.00, 75.00),
    # Google (OpenAI-compatible endpoint)
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.10, 0.40),
    # Mistral
    "mistral-small": (0.10, 0.30),
    "mistral-medium": (0.40, 2.00),
    "mistral-large": (2.00, 6.00),
}

# Model ids that are not chat models (images, audio, embeddings, moderation...).
NOT_CHAT = (
    "embed", "tts", "whisper", "dall-e", "image", "audio", "moderation", "realtime",
    "transcribe", "search", "davinci", "babbage", "computer-use", "sora", "veo", "imagen",
)  # fmt: skip


def suggested_price(model_id: str) -> tuple[float, float] | None:
    m = model_id.lower().removeprefix("models/")
    best = max((p for p in LIST_PRICES if m.startswith(p)), key=len, default=None)
    return LIST_PRICES[best] if best else None


def is_chat_model(model_id: str) -> bool:
    m = model_id.lower()
    return not any(x in m for x in NOT_CHAT)


# A public, daily-maintained price list for thousands of models (the LiteLLM project).
# Providers have no price API of their own, so this is the closest to "live" prices.
PRICE_FEED_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
FEED_SOURCE = "Public price list (LiteLLM, updated daily)"
BUILT_IN_SOURCE = "Captain's built-in list price"
PREFIX = {"gemini": "gemini/", "mistral": "mistral/", "azure_openai": "azure/"}


class PriceFeed:
    """Fetches the public price list at most once a day; without it (offline, switched
    off with an empty URL) the built-in list above is used."""

    def __init__(self, client: object, url: str = PRICE_FEED_URL, ttl_s: float = 86400) -> None:
        self._client, self._url, self._ttl = client, url, ttl_s
        self._data: dict[str, dict[str, object]] = {}
        self._at = -1e18

    def _load(self) -> None:
        import time

        if not self._url or time.monotonic() - self._at < self._ttl:
            return
        self._at = time.monotonic()  # one try per period, even if it fails
        try:
            r = self._client.get(self._url, timeout=15)  # type: ignore[attr-defined]
            if r.status_code == 200:
                self._data = r.json()
        except Exception as e:  # noqa: BLE001 (offline: fall back to the built-in list)
            import logging

            logging.getLogger("captain.prices").info("price list not reachable: %s", e)

    def price(self, kind: str, model_id: str) -> tuple[tuple[float, float], str] | None:
        """((input, output) US$ per 1M tokens, source) or None when no price is known."""
        self._load()
        m = model_id.removeprefix("models/")
        for key in (f"{PREFIX.get(kind, '')}{m}", m):
            v = self._data.get(key)
            if isinstance(v, dict):
                i, o = v.get("input_cost_per_token"), v.get("output_cost_per_token")
                if isinstance(i, int | float) and isinstance(o, int | float):
                    return (round(i * 1e6, 6), round(o * 1e6, 6)), FEED_SOURCE
        p = suggested_price(m)
        return (p, BUILT_IN_SOURCE) if p else None
