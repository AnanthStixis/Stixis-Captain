"""Backup / restore drill: prove a database backup restores into a working Captain, and the
evidence ledger still verifies.

    .venv\\Scripts\\python scripts/restore_drill.py --scratch-url postgresql+psycopg://USER:PASS@HOST/captain_restore_test

What it does, in order (read-only against the source database):
1. pg_dump the SOURCE database (CAPTAIN_DATABASE_URL from backend/.env, or --source-url).
2. pg_restore it into the SCRATCH database, which must already exist and be EMPTY.
3. Run the real ledger verification (hash chain + signatures + anchors) on the restored copy.
4. Compare the restored ledger to the source: entries per partition and the newest hash.
Prints PASS or FAIL for each step and exits 1 on any failure.

Safety: it refuses to run if the scratch database is the source database or is not empty,
and never writes to the source. The database passwords are read from the URLs you give;
type them in your own terminal, never into chat. Needs pg_dump / pg_restore (the PostgreSQL
client tools); pass --pg-bin if they are not on PATH.

Not run in CI (needs PostgreSQL client tools and two databases). Rehearse it before the pilot.
"""

# ruff: noqa: S603
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from captain.config import build_from_settings, load_settings  # noqa: E402
from captain.envfile import DEFAULT_ENV_FILE, dotenv_values  # noqa: E402


def libpq(url: str) -> tuple[list[str], dict[str, str]]:
    """Connection arguments for the PostgreSQL tools, with the password in PGPASSWORD so it
    never appears in a process list."""
    u = make_url(url)
    if not u.database:
        raise SystemExit("the database URL needs a database name")
    args = ["-h", u.host or "localhost", "-p", str(u.port or 5432), "-U", u.username or "postgres"]
    env = {**os.environ, "PGPASSWORD": u.password or ""}
    return [*args, "-d", u.database], env


def find_tool(name: str, pg_bin: Path | None) -> str:
    if pg_bin:
        for cand in (pg_bin / name, pg_bin / f"{name}.exe"):
            if cand.exists():
                return str(cand)
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"{name} not found: put PostgreSQL's bin folder on PATH or pass --pg-bin")
    return found


def ledger_shape(url: str) -> tuple[dict[str, int], dict[str, str]]:
    """(entries per partition, newest entry hash per partition)."""
    eng = create_engine(url)
    with eng.connect() as c:
        counts = {
            str(p): int(n)
            for p, n in c.execute(
                text("select partition, count(*) from evidence_ledger group by partition")
            )
        }
        last = {
            str(p): str(h)
            for p, h in c.execute(
                text(
                    "select distinct on (partition) partition, entry_hash "
                    "from evidence_ledger order by partition, seq desc"
                )
            )
        }
    eng.dispose()
    return counts, last


def is_empty(url: str) -> bool:
    eng = create_engine(url)
    with eng.connect() as c:
        n = c.execute(
            text("select count(*) from information_schema.tables where table_schema = 'public'")
        ).scalar_one()
    eng.dispose()
    return int(n or 0) == 0


def compare(
    src: tuple[dict[str, int], dict[str, str]], dst: tuple[dict[str, int], dict[str, str]]
) -> list[str]:
    """Differences between the source and restored ledger (empty list = identical)."""
    problems = []
    for part in sorted(set(src[0]) | set(dst[0])):
        if src[0].get(part) != dst[0].get(part):
            problems.append(
                f"partition {part}: {src[0].get(part)} in source, {dst[0].get(part)} restored"
            )
        elif src[1].get(part) != dst[1].get(part):
            problems.append(f"partition {part}: newest entry hash differs")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--source-url")
    ap.add_argument("--scratch-url", required=True)
    ap.add_argument("--pg-bin", type=Path)
    a = ap.parse_args(argv)
    env: Mapping[str, str] = {**dotenv_values(DEFAULT_ENV_FILE), **os.environ}
    source = a.source_url or env.get("CAPTAIN_DATABASE_URL")
    if not source:
        raise SystemExit("no source database: set CAPTAIN_DATABASE_URL or pass --source-url")
    su, du = make_url(source), make_url(a.scratch_url)
    if (su.host, su.port, su.database) == (du.host, du.port, du.database):
        raise SystemExit("REFUSED: the scratch database is the source database")
    results: list[tuple[str, bool, str]] = []

    def step(name: str, ok: bool, detail: str = "") -> bool:
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")
        return ok

    if not step("scratch database is empty", is_empty(a.scratch_url)):
        return 1
    pg_dump, pg_restore = find_tool("pg_dump", a.pg_bin), find_tool("pg_restore", a.pg_bin)
    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "captain.dump"
        args, penv = libpq(source)
        r = subprocess.run(
            [pg_dump, "-Fc", "-f", str(dump), *args], env=penv, capture_output=True, text=True
        )
        if not step(
            "pg_dump of the source",
            r.returncode == 0,
            r.stderr.strip()[:200] or f"{dump.stat().st_size} bytes",
        ):
            return 1
        args, penv = libpq(a.scratch_url)
        r = subprocess.run(
            [pg_restore, "--no-owner", "--no-privileges", *args, str(dump)],
            env=penv,
            capture_output=True,
            text=True,
        )
        if not step(
            "pg_restore into the scratch database", r.returncode == 0, r.stderr.strip()[:200]
        ):
            return 1
    same = compare(ledger_shape(source), ledger_shape(a.scratch_url))
    step("restored ledger matches the source", not same, "; ".join(same))
    settings = load_settings({**env, "CAPTAIN_DATABASE_URL": a.scratch_url})
    built = build_from_settings(settings)
    rep = built.core.m.ledger.verify()
    step(
        "ledger verifies on the restored copy",
        rep.ok,
        f"{rep.entries_checked} entries, {rep.anchors_checked} anchors, "
        f"problems: {len(rep.problems)}",
    )
    eng = create_engine(a.scratch_url)
    with eng.connect() as c:
        n = c.execute(select(func.count()).select_from(text("register_entries"))).scalar_one()
    step("register entries present", int(n) > 0, f"{n} entries")
    bad = [n for n, ok, _ in results if not ok]
    print("\nDRILL " + ("PASSED" if not bad else f"FAILED ({', '.join(bad)})"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
