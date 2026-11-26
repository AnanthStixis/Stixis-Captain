"""Provider client: OpenAI-compatible Chat Completions over HTTP.

One client covers every provider:
- LiteLLM proxy (recommended): set the registry provider's base_url to the LiteLLM URL;
  LiteLLM translates to Anthropic, Gemini, Bedrock, Azure, Mistral, … The LiteLLM
  `model_name` must equal the registry entry's `provider_model`.
- OpenAI (or any OpenAI-compatible endpoint) directly: base_url = https://api.openai.com/v1.

Credentials are looked up at call time from the target's credential_ref by a
SecretResolver (env:NAME in development; the client's vault in production). Captain
never stores provider keys, and never logs them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from typing import Any, Protocol

import httpx

from captain.core.models import ProviderTarget

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None, retryable: bool) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class SecretResolver(Protocol):
    def secret(self, ref: str) -> str: ...


class EnvSecretResolver:
    """Development resolver: `env:NAME` reads NAME from `env` (default: the process
    environment). Production uses a vault-backed resolver with the same protocol."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = env if env is not None else os.environ

    def secret(self, ref: str) -> str:
        kind, _, name = ref.partition(":")
        if kind != "env" or not name:
            raise ProviderError(
                f"credential ref {ref!r} is not supported by EnvSecretResolver", None, False
            )
        v = self._env.get(name)
        if not v:
            raise ProviderError(f"credential {ref} is not set", None, False)
        return v


class StoredSecretResolver:
    """`captain:<id>`: a company AI key entered on the dashboard and stored encrypted
    (captain.core.secrets_store). Decrypted here, at call time, and never logged."""

    def __init__(self, secrets: Any) -> None:  # SecretsService
        self._s = secrets

    def secret(self, ref: str) -> str:
        kind, _, sid = ref.partition(":")
        if kind != "captain" or not sid:
            raise ProviderError(f"credential ref {ref!r} is not a stored AI key", None, False)
        try:
            return str(self._s.reveal(sid))
        except KeyError as e:
            raise ProviderError("the AI key for this provider was removed", None, False) from e
        except Exception as e:  # master key unavailable: fail closed, no call is made
            raise ProviderError(f"the AI key could not be decrypted ({type(e).__name__})",
                                None, False) from e  # fmt: skip


class ChainSecretResolver:
    """Picks the resolver by the ref's prefix (`env:`, `captain:`, ...)."""

    def __init__(self, by_kind: Mapping[str, SecretResolver]) -> None:
        self._by = dict(by_kind)

    def secret(self, ref: str) -> str:
        kind = ref.partition(":")[0]
        r = self._by.get(kind)
        if r is None:
            raise ProviderError(f"credential ref {ref!r}: no resolver for {kind!r}", None, False)
        return r.secret(ref)


class ProviderClient(Protocol):
    def complete(self, target: ProviderTarget, body: dict[str, Any]) -> dict[str, Any]: ...
    def stream(self, target: ProviderTarget, body: dict[str, Any]) -> Iterator[dict[str, Any]]: ...


class OpenAICompatibleClient:
    def __init__(
        self,
        secrets: SecretResolver,
        timeout_s: float = 120.0,
        connect_timeout_s: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._secrets = secrets
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s), transport=transport
        )

    def _req(
        self, target: ProviderTarget, body: dict[str, Any], path: str = "/chat/completions"
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        if not target.base_url:
            raise ProviderError(f"provider {target.provider_id} has no base_url", None, False)
        url = target.base_url.rstrip("/") + path
        headers = {"Authorization": f"Bearer {self._secrets.secret(target.credential_ref)}"}
        return url, headers, {**body, "model": target.provider_model}

    @staticmethod
    def _fail(status: int, text: str) -> ProviderError:
        # Provider error bodies can echo input; keep only a short, safe prefix.
        return ProviderError(
            f"provider HTTP {status}: {text[:200]}", status, status in RETRYABLE_STATUS
        )

    def complete(self, target: ProviderTarget, body: dict[str, Any]) -> dict[str, Any]:
        url, headers, payload = self._req(target, {**body, "stream": False})
        try:
            r = self._client.post(url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise ProviderError(f"provider unreachable: {type(e).__name__}", None, True) from e
        if r.status_code >= 400:
            raise self._fail(r.status_code, r.text)
        data: dict[str, Any] = r.json()
        return data

    def embed(self, target: ProviderTarget, body: dict[str, Any]) -> dict[str, Any]:
        """OpenAI-compatible POST {base_url}/embeddings. Not part of ProviderClient: a
        provider that cannot embed simply lacks this method."""
        url, headers, payload = self._req(target, body, "/embeddings")
        try:
            r = self._client.post(url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise ProviderError(f"provider unreachable: {type(e).__name__}", None, True) from e
        if r.status_code >= 400:
            raise self._fail(r.status_code, r.text)
        data: dict[str, Any] = r.json()
        return data

    def stream(self, target: ProviderTarget, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Yields parsed chunk objects. Errors before the first chunk are retryable
        (nothing was sent to the caller yet); errors mid-stream are not."""
        url, headers, payload = self._req(
            target, {**body, "stream": True, "stream_options": {"include_usage": True}}
        )
        started = False
        try:
            with self._client.stream("POST", url, headers=headers, json=payload) as r:
                if r.status_code >= 400:
                    raise self._fail(r.status_code, r.read().decode(errors="replace"))
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    started = True
                    yield json.loads(data)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise ProviderError(
                f"provider stream failed: {type(e).__name__}", None, not started
            ) from e
