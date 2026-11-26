"""Scan the files that would be committed for secrets (MVP acceptance: no secrets in code).

    .venv\\Scripts\\python scripts/secret_scan.py

Two checks over every file git would commit (tracked plus new, minus .gitignore):
1. YOUR OWN SECRETS: every value in backend/.env (pepper, signing key, provider keys, tokens,
   client secrets) is searched for, verbatim, in the other files. A hit is a real leak and has
   no false positives.
2. KNOWN SECRET SHAPES: provider and cloud keys, private key blocks, bearer tokens, JWTs and
   "password = '...'" assignments. Shapes can match harmless text, so each hit is shown
   (value masked) for a human to judge; known fake test fixtures are skipped.

Output never contains a full secret. Exit code 1 if anything is found. This complements, and
does not replace, the gitleaks job in CI (which also scans git history, which this does not).
"""

# ruff: noqa: S607
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV = ROOT / "backend" / ".env"
FAKES = ("AKIAABCDEFGHIJKLMNOP", "AKIAIOSFODNN7EXAMPLE", "cpt_live_wrong", "sk-test-not-a-real-key")
SHAPES: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI-style key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    (
        "Captain virtual key",
        re.compile(r"\bcpt_live_[A-Za-z0-9]{6,}_[A-Za-z0-9]{16,}_[A-Za-z0-9]+"),
    ),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    (
        "Assigned secret",
        re.compile(
            r"""(?i)\b(?:password|passwd|secret|api[_-]?key|token|client_secret)\b\s*[:=]\s*"""
            r"""['"]([^'"\s]{16,})['"]"""
        ),
    ),
]
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".zip", ".xlsx", ".pdf",
               ".vsix", ".dll", ".exe", ".lock", ".pyc"}  # fmt: skip
SKIP_NAMES = {"package-lock.json"}
# Fixed, public dev constants for the MOCK servers (written into .env by `captain.devsetup`
# and kept in code on purpose; one is only a key id). Not secrets: skipped by name.
PUBLIC_DEV_VALUES = {"CAPTAIN_MOCK_MCP_KEY", "CAPTAIN_MOCK_PROVIDER_KEY", "CAPTAIN_LEDGER_KEY_ID"}


def candidate_files() -> list[Path]:
    out = subprocess.run(  # noqa: S603 - fixed git command
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    files = []
    for name in filter(None, out.split("\0")):
        p = ROOT / name
        if p.suffix.lower() in SKIP_SUFFIX or p.name in SKIP_NAMES or not p.is_file():
            continue
        if p.stat().st_size > 2_000_000:
            continue
        files.append(p)
    return files


def own_secrets() -> list[tuple[str, str]]:
    """(name, value) pairs from backend/.env that look like secrets."""
    if not ENV.exists():
        return []
    pairs = []
    for line in ENV.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.strip().partition("=")
        v = v.strip().strip('"').strip("'")
        if (
            sep
            and not line.lstrip().startswith("#")
            and len(v) >= 12
            and not v.startswith(("http", "/", "."))
            and k not in PUBLIC_DEV_VALUES
        ):
            if any(
                w in k.upper()
                for w in ("KEY", "SECRET", "TOKEN", "PASSWORD", "PEPPER", "DATABASE_URL")
            ):
                pairs.append((k, v))
    return pairs


def mask(v: str) -> str:
    return v[:4] + "…" + f"({len(v)} chars)" if len(v) > 8 else "…"


def scan() -> int:
    files = candidate_files()
    findings = 0
    secrets = own_secrets()
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = p.relative_to(ROOT)
        for name, value in secrets:
            if value in text:
                print(f"LEAK  {rel}: contains the value of {name} from backend/.env")
                findings += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(f in line for f in FAKES):
                continue
            for label, rx in SHAPES:
                if label == "Assigned secret" and rel.parts[:2] in (("backend", "tests"),):
                    continue  # fake test passwords on purpose; real key shapes still scan
                m = rx.search(line)
                if m:
                    shown = m.group(1) if m.groups() else m.group(0)
                    print(f"SHAPE {rel}:{lineno}: {label}: {mask(shown)}")
                    findings += 1
    print(
        f"\nscanned {len(files)} files against {len(secrets)} secrets from backend/.env and "
        f"{len(SHAPES)} known shapes: "
        + ("NOTHING FOUND" if not findings else f"{findings} finding(s) to review")
    )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(scan())
