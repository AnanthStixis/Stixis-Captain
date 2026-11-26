"""Helpers of the real-provider setup script (the interactive part is run by a person)."""

from __future__ import annotations

import pytest

from captain.devrealprovider import check_base_url, env_var_for, per_million


def test_prices_become_captain_money_units_exactly() -> None:
    assert per_million("0.15") == 150_000
    assert per_million("$3") == 3_000_000
    assert per_million(" 15.00 ") == 15_000_000
    assert per_million("0") == 0
    for bad in ["abc", "", "-1"]:
        with pytest.raises(ValueError):
            per_million(bad)


def test_the_registry_stores_a_variable_name_not_a_key() -> None:
    assert env_var_for("openai") == "CAPTAIN_PROVIDER_KEY_OPENAI"
    assert env_var_for("open-router.ai") == "CAPTAIN_PROVIDER_KEY_OPEN_ROUTER_AI"


def test_provider_url_must_be_https_or_local() -> None:
    assert check_base_url("https://api.openai.com/v1/") == "https://api.openai.com/v1"
    assert check_base_url("http://127.0.0.1:4000/v1") == "http://127.0.0.1:4000/v1"
    for bad in ["http://api.example.com/v1", "ftp://x", "api.openai.com"]:
        with pytest.raises(ValueError):
            check_base_url(bad)
