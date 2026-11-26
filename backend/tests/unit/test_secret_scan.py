"""The secret scanner must actually catch secrets (a scanner that finds nothing could be broken)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import secret_scan as S  # noqa: E402


def hits(line: str) -> set[str]:
    return {label for label, rx in S.SHAPES if rx.search(line)}


def test_known_secret_shapes_are_caught() -> None:
    # Built at runtime so this file itself contains no secret-shaped literal.
    assert "OpenAI-style key" in hits("key = " + "sk-" + "a" * 30)
    assert "Anthropic key" in hits("sk-" + "ant-" + "b" * 30)
    assert "AWS access key id" in hits("AK" + "IA" + "Q" * 16)
    assert "Google API key" in hits("AI" + "za" + "c" * 35)
    assert "GitHub token" in hits("gh" + "p_" + "d" * 36)
    assert "Private key block" in hits("-----BEGIN " + "RSA PRIVATE KEY-----")
    assert "Captain virtual key" in hits("cpt_live_" + "abc123_" + "x" * 20 + "_ff00")
    jwt = "ey" + "J" + "a" * 12 + "." + "ey" + "J" + "b" * 12 + "." + "c" * 12
    assert "JWT" in hits(jwt)
    assert "Assigned secret" in hits('client_secret = "' + "z" * 20 + '"')


def test_ordinary_text_is_not_flagged() -> None:
    for line in ("api_key = os.environ['X']", "the token budget is 5", "password: str", "sk-short"):
        assert not hits(line), line


def test_values_from_env_are_masked_in_output() -> None:
    assert S.mask("supersecretvalue123") == "supe…(19 chars)"
    assert "secretvalue" not in S.mask("supersecretvalue123")
