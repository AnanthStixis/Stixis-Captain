"""Connect a REAL model provider to the local Captain gateway:
    python -m captain.devrealprovider

Run it in YOUR terminal. It asks for the provider's details and API key (hidden), then:
- saves the key in backend/.env (gitignored) under a variable name; the registry only
  stores that variable NAME (credential_ref), never the key;
- registers the provider and a model alias through the audited admin path;
- adds a price so budgets and cost reports work (you supply it: check your provider's
  pricing page; Captain does not guess prices);
- lets the demo application (AI-DEV) use the new alias.

Any OpenAI-compatible endpoint works (OpenAI, OpenRouter, Groq, a LiteLLM proxy, ...).
Prompts sent through the gateway then LEAVE this machine and go to that provider, and the
provider bills you. Use harmless prompts while testing. Restart the gateway afterwards.
"""

from __future__ import annotations

import getpass
import re
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from captain.config import build_from_env
from captain.core.admin import AdminService
from captain.core.registry.service import ModelEntry, Provider, ProviderKind
from captain.core.usage.pricing import ModelPrice
from captain.db.stores import save_price
from captain.devseed import SEED_ADMIN, infra_engine
from captain.envfile import DEFAULT_ENV_FILE
from captain.sso_setup import update_env


def env_var_for(provider_id: str) -> str:
    """openrouter -> CAPTAIN_PROVIDER_KEY_OPENROUTER (the NAME stored in the registry)."""
    return "CAPTAIN_PROVIDER_KEY_" + re.sub(r"[^A-Za-z0-9]+", "_", provider_id).strip("_").upper()


def per_million(usd: str) -> int:
    """'0.15' dollars per million tokens -> Captain money units (millionths of a dollar)."""
    try:
        v = Decimal(usd.strip().lstrip("$"))
    except InvalidOperation as e:
        raise ValueError(f"not a number: {usd!r}") from e
    if v < 0:
        raise ValueError("a price cannot be negative")
    return int(v * 1_000_000)


def check_base_url(url: str) -> str:
    u = url.strip().rstrip("/")
    if not (
        u.startswith("https://")
        or u.startswith("http://127.0.0.1")
        or u.startswith("http://localhost")
    ):
        raise ValueError("the provider URL must be https (or a proxy on this machine)")
    return u


def main() -> int:
    print("Connect a real model provider (OpenAI-compatible).\n")
    print("Your prompts will leave this machine and the provider will bill your account.")
    if input("Continue? [y/N] ").strip().lower() != "y":
        return 1
    try:
        pid = input("Provider name, e.g. openai [openai]: ").strip() or "openai"
        base = check_base_url(
            input("Base URL, ending in /v1 [https://api.openai.com/v1]: ").strip()
            or "https://api.openai.com/v1"
        )
        pmodel = input("The provider's model id, e.g. gpt-4o-mini: ").strip()
        alias = input(f"Name to use in the IDE [{pmodel}]: ").strip() or pmodel
        print("Prices in US dollars per MILLION tokens (see your provider's pricing page).")
        in_m = per_million(input("  input price per million tokens: "))
        out_m = per_million(input("  output price per million tokens: "))
        key = getpass.getpass("API key (hidden): ").strip()
    except ValueError as e:
        print(f"\n{e}")
        return 2
    if not pmodel or not key or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", alias):
        print("\nmodel id, a simple alias and an API key are required")
        return 2

    var = env_var_for(pid)
    update_env(DEFAULT_ENV_FILE, {var: key})

    built, _ = build_from_env()
    admin = AdminService(built.core, built.register, built.infra.budget_catalog)  # type: ignore[arg-type]
    admin.upsert_registry(
        SEED_ADMIN, Provider(pid, ProviderKind.OPENAI, "external", f"env:{var}", base_url=base)
    )
    admin.upsert_registry(SEED_ADMIN, ModelEntry(alias, pid, pmodel, "external"))
    version = "dev-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    try:
        save_price(
            infra_engine(),
            ModelPrice(alias, pid, version, datetime.now(UTC) - timedelta(minutes=1), in_m, out_m),
        )
    except IntegrityError:
        print("a price for this alias already exists; keeping it")
    entry = built.register.get("AI-DEV")
    if entry is not None and alias not in entry.allowed_models:
        admin.upsert_register(
            SEED_ADMIN,
            entry.model_copy(
                update={
                    "allowed_models": entry.allowed_models | {alias},
                    "version": entry.version + 1,
                }
            ),
        )
    print(
        f"\nDone. The key is in {DEFAULT_ENV_FILE} as {var} (not shown).\n"
        f"Restart the gateway, then use '{alias}' as the model id in your IDE config."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
