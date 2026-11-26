"""Control-mapped evidence export (FR-16) that an auditor can verify without Stixis.

A package contains:
- controls.csv   — per control: how many ledger events evidence it in the period
- events.jsonl   — the ledger entries themselves (hashes, signatures, key ids; never
                   prompt content), so they can be re-verified independently
- manifest.json  — framework, control-map version, period, per-partition sequence
                   ranges and last hashes, covering anchor ids, and SHA-256 of each file

`verify_package()` needs only the package and the public signing keys: it checks file
hashes, recomputes every entry hash, checks the chain is unbroken within each
partition range, and checks every signature. Matching the anchor ids against the WORM
store proves the entries existed when anchored.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from typing import Any

from captain.core.ledger.chain import LedgerEntry, Verifier, compute_entry_hash

FRAMEWORKS = ("NIST_AI_RMF", "ISO_42001", "EU_AI_ACT")


def load_control_map() -> dict[str, Any]:
    text = resources.files("captain.core.reporting").joinpath("control_map.json").read_text("utf-8")
    m: dict[str, Any] = json.loads(text)
    return m


@dataclass(frozen=True)
class EvidencePackage:
    manifest: dict[str, Any]
    files: dict[str, bytes]  # name -> content


def _matches(control: dict[str, Any], e: LedgerEntry) -> bool:
    if e.event_type in control.get("events", ()):
        return True
    reasons = control.get("refusal_reasons", ())
    return e.event_type == "call.refused" and e.body.get("reason_code") in reasons


def _entry_json(e: LedgerEntry) -> dict[str, Any]:
    return {
        "partition": e.partition,
        "seq": e.seq,
        "event_id": e.event_id,
        "event_type": e.event_type,
        "recorded_at": e.recorded_at.astimezone(UTC).isoformat(timespec="microseconds"),
        "body": e.body,
        "prev_hash": e.prev_hash,
        "entry_hash": e.entry_hash,
        "signature": e.signature,
        "key_id": e.key_id,
        "anchor_id": e.anchor_id,
    }


def export_evidence(
    entries: list[LedgerEntry], framework: str, start: datetime, end: datetime
) -> EvidencePackage:
    if framework not in FRAMEWORKS:
        raise ValueError(f"framework must be one of {FRAMEWORKS}")
    cmap = load_control_map()
    controls = cmap["frameworks"][framework]["controls"]
    picked = sorted(
        (e for e in entries if start <= e.recorded_at < end), key=lambda e: (e.partition, e.seq)
    )
    rows = []
    for c in controls:
        hits = [e for e in picked if _matches(c, e)]
        rows.append(
            {
                "control_id": c["id"],
                "control": c["name"],
                "evidence_events": len(hits),
                "first_event": hits[0].event_id if hits else "",
                "last_event": hits[-1].event_id if hits else "",
            }
        )
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    controls_csv = buf.getvalue().encode()
    events_jsonl = "".join(
        json.dumps(_entry_json(e), sort_keys=True) + "\n" for e in picked
    ).encode()

    parts: dict[str, dict[str, Any]] = defaultdict(dict)
    for e in picked:
        p = parts[e.partition]
        p.setdefault("first_seq", e.seq)
        p["last_seq"], p["last_entry_hash"] = e.seq, e.entry_hash
        p["count"] = p.get("count", 0) + 1
    files = {"controls.csv": controls_csv, "events.jsonl": events_jsonl}
    manifest = {
        "framework": framework,
        "framework_name": cmap["frameworks"][framework]["name"],
        "control_map_version": cmap["version"],
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "generated_at": datetime.now(UTC).isoformat(),
        "events": len(picked),
        "partitions": dict(sorted(parts.items())),
        "anchor_ids": sorted({e.anchor_id for e in picked if e.anchor_id}),
        "unanchored_events": sum(1 for e in picked if not e.anchor_id),
        "files": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()},
    }
    return EvidencePackage(manifest, files)


@dataclass
class PackageCheck:
    ok: bool
    problems: list[str]


def verify_package(pkg: EvidencePackage, verifier: Verifier) -> PackageCheck:
    problems = []
    for name, digest in pkg.manifest["files"].items():
        if hashlib.sha256(pkg.files.get(name, b"")).hexdigest() != digest:
            problems.append(f"{name}: SHA-256 does not match manifest")
    prev: dict[str, tuple[int, str]] = {}
    for line in pkg.files["events.jsonl"].decode().splitlines():
        r = json.loads(line)
        e = LedgerEntry(
            r["partition"],
            r["seq"],
            r["event_id"],
            r["event_type"],
            datetime.fromisoformat(r["recorded_at"]),
            r["body"],
            r["prev_hash"],
            r["entry_hash"],
            r["signature"],
            r["key_id"],
            r["anchor_id"],
        )
        where = f"{e.partition}#{e.seq}"
        if compute_entry_hash(e.prev_hash, e.hashed_part()) != e.entry_hash:
            problems.append(f"{where}: entry hash mismatch")
        if not verifier.verify(
            e.key_id, bytes.fromhex(e.entry_hash), base64.b64decode(e.signature)
        ):
            problems.append(f"{where}: invalid signature")
        if e.partition in prev:
            pseq, phash = prev[e.partition]
            if e.seq == pseq + 1 and e.prev_hash != phash:
                problems.append(f"{where}: chain broken")
        prev[e.partition] = (e.seq, e.entry_hash)
    for part, info in pkg.manifest["partitions"].items():
        if prev.get(part, (None, None))[1] != info["last_entry_hash"]:
            problems.append(f"{part}: last entry hash differs from manifest")
    return PackageCheck(not problems, problems)
