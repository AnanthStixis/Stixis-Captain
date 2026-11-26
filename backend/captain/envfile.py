"""Minimal .env reader for local development (KEY=value lines, # comments).

Real environment variables always take precedence. In production, settings come from
the secret store as environment variables and no .env file exists.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


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
