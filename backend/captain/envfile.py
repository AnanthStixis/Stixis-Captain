"""Minimal .env reader for local development (KEY=value lines, # comments).

Real environment variables always take precedence. In production, settings come from
the secret store as environment variables and no .env file exists.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

# Shared Captain (one deployment, many companies): while a company's services are being
# built, its own settings (database schema, keys, public address...) overlay the process
# settings. Context-local, so concurrent builds for different companies never mix.
_overlay: ContextVar[Mapping[str, str] | None] = ContextVar("captain_env_overlay", default=None)


def current_env() -> dict[str, str]:
    """backend/.env (development), then the real environment, then the company overlay."""
    env = {**dotenv_values(DEFAULT_ENV_FILE), **os.environ}
    extra = _overlay.get()
    if extra:
        env.update(extra)
    return env


@contextmanager
def tenant_env(overrides: Mapping[str, str]) -> Iterator[None]:
    token = _overlay.set(dict(overrides))
    try:
        yield
    finally:
        _overlay.reset(token)


def dotenv_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out
