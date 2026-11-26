"""Generic export importer for vendors without a verified API here (Cursor, ChatGPT
Enterprise, others): an administrator downloads the vendor's usage export and maps its
columns. Nothing about any vendor's export format is assumed; the mapping says it.

mapping = {
  "vendor": "cursor",                 # label recorded as the source
  "day": "Date",                      # column holding the date (YYYY-MM-DD or ISO datetime)
  "actor": "Email",                   # optional
  "model": "Model",                   # optional
  "input_tokens": "Input Tokens",     # optional
  "output_tokens": "Output Tokens",   # optional
  "cost_usd": "Cost",                 # optional, dollars
  "id_columns": ["Date", "Email", "Model"],   # columns that make a row unique
  "facts": {"requests": "Requests"},          # optional extra counters (numbers)
}
Rows whose id columns repeat are the same record: importing again changes nothing.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from typing import Any

from captain.core.models import TokenUsage
from captain.integrations.telemetry.base import Facts, ImportFailure, ObservedRecord


def _int(v: str | None) -> int:
    try:
        return int(float((v or "0").replace(",", "")))
    except ValueError:
        return 0


def _day(v: str) -> date:
    try:
        return datetime.fromisoformat(v.strip().replace("Z", "+00:00")).date()
    except ValueError as e:
        raise ImportFailure(f"unreadable date: {v!r}") from e


def read_csv(text: str, mapping: Mapping[str, Any]) -> Iterator[ObservedRecord]:
    vendor = str(mapping.get("vendor") or "")
    if not vendor.replace("_", "").replace("-", "").isalnum():
        raise ImportFailure("mapping needs a simple 'vendor' label")
    ids = list(mapping.get("id_columns") or [])
    day_col = mapping.get("day")
    if not ids or not day_col:
        raise ImportFailure("mapping needs 'day' and 'id_columns'")
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in [day_col, *ids] if c not in (reader.fieldnames or [])]
    if missing:
        raise ImportFailure(f"columns not found in the file: {', '.join(missing)}")
    for row in reader:
        facts: Facts = {k: _int(row.get(col)) for k, col in (mapping.get("facts") or {}).items()}
        tokens = None
        if mapping.get("input_tokens") or mapping.get("output_tokens"):
            tokens = TokenUsage(
                input_tokens=_int(row.get(mapping.get("input_tokens") or "")),
                output_tokens=_int(row.get(mapping.get("output_tokens") or "")),
            )
        cost = None
        if mapping.get("cost_usd"):
            try:
                cost = round(float((row.get(mapping["cost_usd"]) or "0").replace(",", "")) * 1e6)
            except ValueError:
                cost = None
        yield ObservedRecord(
            f"csv:{vendor}",
            "|".join(row.get(c) or "" for c in ids),
            _day(row[day_col]),
            row.get(mapping.get("actor") or "") or None,
            model=row.get(mapping.get("model") or "") or None,
            tokens=tokens,
            cost_micro=cost,
            facts=facts,
        )
