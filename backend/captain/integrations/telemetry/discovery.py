"""Discovery of AI use that goes AROUND Captain: BYPASS_DETECTED evidence.

Sources are logs an administrator already has: a web proxy or DNS log (who reached which
domain) or an identity-provider report (who signed in to which application). The admin
exports them to CSV and maps the columns, exactly like the vendor export importer; nothing
about any product's log format is assumed.

What "bypass detected" means, honestly: the log says this person's traffic reached a known
AI service directly. It does NOT say what was sent, whether company data was involved, or
whether the use was allowed. It is a signal to follow up, never a governed call: it never
touches a budget and never counts as spend. Traffic to Captain's own gateway is skipped.

The built-in service list is a starting point (well-known public AI services) and is meant
to be extended with `extra_services` in the mapping. Matching is by domain suffix or an
exact application name, case-insensitive.

mapping = {
  "source": "proxy",                  # label: proxy | dns | idp | ...
  "day": "Date", "actor": "User",     # actor optional
  "target": "Domain",                 # domain, URL host or application name
  "count": "Requests",                # optional number of requests / sign-ins
  "id_columns": ["Date", "User", "Domain"],
  "captain_hosts": ["captain.example.com"],     # never reported as a bypass
  "extra_services": {"intranet-ai.example.com": "Internal AI tool"}   # optional
}
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlparse

from captain.core.models import CoverageClass
from captain.integrations.telemetry.base import Facts, ImportFailure, ObservedRecord
from captain.integrations.telemetry.csv_import import _day, _int

# Public AI services commonly seen in enterprises. Not exhaustive; extend per deployment.
DEFAULT_SERVICES: dict[str, str] = {
    "chatgpt.com": "ChatGPT",
    "openai.com": "OpenAI",
    "claude.ai": "Claude",
    "anthropic.com": "Anthropic",
    "gemini.google.com": "Gemini",
    "aistudio.google.com": "Google AI Studio",
    "copilot.microsoft.com": "Microsoft Copilot",
    "perplexity.ai": "Perplexity",
    "poe.com": "Poe",
    "character.ai": "Character.AI",
    "cursor.com": "Cursor",
    "cursor.sh": "Cursor",
    "mistral.ai": "Mistral",
    "huggingface.co": "Hugging Face",
    "deepseek.com": "DeepSeek",
    "grok.com": "Grok",
    "x.ai": "xAI",
}


def _host(v: str) -> str:
    v = v.strip().lower()
    if "://" in v:
        v = urlparse(v).hostname or v
    return v.split("/")[0].split(":")[0].rstrip(".")


def classify(target: str, services: Mapping[str, str], captain_hosts: set[str]) -> str | None:
    """The AI service name if `target` is a known AI service that is not Captain."""
    host = _host(target)
    if not host or any(host == c or host.endswith("." + c) for c in captain_hosts):
        return None
    for key, name in services.items():
        k = key.lower()
        if host == k or host.endswith("." + k):
            return name
    for name in services.values():  # an application name from an IdP report
        if target.strip().lower() == name.lower():
            return name
    return None


def read_discovery(text: str, mapping: Mapping[str, Any]) -> Iterator[ObservedRecord]:
    src = str(mapping.get("source") or "")
    if not src.replace("_", "").replace("-", "").isalnum():
        raise ImportFailure("mapping needs a simple 'source' label")
    ids = list(mapping.get("id_columns") or [])
    day_col, target_col = mapping.get("day"), mapping.get("target")
    if not ids or not day_col or not target_col:
        raise ImportFailure("mapping needs 'day', 'target' and 'id_columns'")
    services = {
        **DEFAULT_SERVICES,
        **{str(k): str(v) for k, v in (mapping.get("extra_services") or {}).items()},
    }
    hosts = {_host(str(h)) for h in (mapping.get("captain_hosts") or [])}
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in [day_col, target_col, *ids] if c not in (reader.fieldnames or [])]
    if missing:
        raise ImportFailure(f"columns not found in the file: {', '.join(missing)}")
    for row in reader:
        service = classify(row.get(target_col) or "", services, hosts)
        if service is None:
            continue  # not an AI service we know, or it is Captain itself
        facts: Facts = {"service": service, "log_source": src}
        if mapping.get("count"):
            facts["requests"] = _int(row.get(mapping["count"]))
        yield ObservedRecord(
            f"discovery:{src}",
            "|".join(row.get(c) or "" for c in ids),
            _day(row[day_col]),
            row.get(mapping.get("actor") or "") or None,
            facts=facts,
            coverage=CoverageClass.BYPASS_DETECTED,
        )
