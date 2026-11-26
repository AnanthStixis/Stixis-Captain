"""Architecture rules from ADR-001, checked on every run.

1. Integrations import only the Core's public contract (decision_api, models).
2. Captain Core never depends on infrastructure adapters (captain.db) or transports
   (captain.api), so it stays testable and swappable.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "captain"
CORE_PUBLIC = {"captain.core.decision_api", "captain.core.models"}


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
        elif isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
    return out


def files(pkg: str) -> list[Path]:
    return sorted((ROOT / pkg).rglob("*.py"))


@pytest.mark.parametrize("path", files("integrations"), ids=lambda p: p.name)
def test_integrations_use_only_core_contract(path: Path) -> None:
    bad = {m for m in imports(path) if m.startswith("captain.core") and m not in CORE_PUBLIC}
    assert not bad, f"{path} imports Core internals: {bad}"


@pytest.mark.parametrize("path", files("core"), ids=lambda p: str(p.relative_to(ROOT)))
def test_core_has_no_infrastructure_dependencies(path: Path) -> None:
    bad = {
        m
        for m in imports(path)
        if m.startswith(("captain.db", "captain.api", "captain.workers", "captain.config"))
    }
    assert not bad, f"{path} depends on {bad}"
