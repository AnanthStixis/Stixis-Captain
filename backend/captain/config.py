"""Build the production Captain Core from environment settings.

Secrets (pepper, SMTP password, database password) are injected as environment
variables by the client's secret store; they are never in code or config files.

| Variable                          | Required | Meaning                                         |
|-----------------------------------|----------|-------------------------------------------------|
| CAPTAIN_DATABASE_URL              | yes      | postgresql+psycopg://user:pw@host:5432/db       |
| CAPTAIN_KEY_PEPPER                | yes      | ≥ 32 bytes as hex; HMAC pepper for virtual keys |
| CAPTAIN_OPA_URL                   |          | default http://127.0.0.1:8181                   |
| CAPTAIN_LEDGER_KEY                | yes      | pem:<path> (dev) or azurekv:<Key Vault key URL>  |
| CAPTAIN_LEDGER_KEY_ID             |          | default "ledger-1"                              |
| CAPTAIN_LEDGER_PUBLIC_KEYS_DIR    |          | <key_id>.pem files of retired keys (rotation)   |
| CAPTAIN_WORM                      | yes      | dir:<path> (dev) · s3:<bucket>[/<prefix>] ·     |
|                                   |          | azure:<account_url>|<container> (production)    |
| CAPTAIN_REQUIRE_BUDGET            |          | default 1: refuse calls no hard budget covers   |
| CAPTAIN_WORM_RETENTION_DAYS       |          | anchor retention, default 400 (min 183)         |
| CAPTAIN_GUARD_URLS                |          | comma list of guard endpoints (≥2 for HA)       |
| CAPTAIN_GUARD_FALLBACK            |          | approved hosted fallback guard endpoint         |
| CAPTAIN_PRESIDIO                  |          | "1" to add Presidio NER (needs the extra)       |
| CAPTAIN_APPROVERS                 |          | JSON {"<register_id>": ["approver", …], "*": […]}|
| CAPTAIN_ESCALATION_APPROVERS      |          | comma list of backup approvers                  |
| CAPTAIN_OIDC_ISSUERS              |          | JSON [{"issuer","audience","jwks_uri"}]         |
| CAPTAIN_SMTP_HOST / _PORT / _FROM |          | alert e-mail; _USER / _PASSWORD optional        |
| CAPTAIN_ALERT_RECIPIENTS          |          | comma list for alerts without own recipients    |
| CAPTAIN_SERVICENOW_URL/_USER/_PASSWORD |     | open ServiceNow incidents for alerts            |
| CAPTAIN_SERVICENOW_MIN_SEVERITY / _GROUP |   | INFO|WARNING|CRITICAL (default) / assignment group|
| CAPTAIN_JUDGE_URL                 |          | in-perimeter OpenAI-compatible judge; unset = no |
|                                   |          | live evaluation (ADR-005). Never a hosted model  |
| CAPTAIN_JUDGE_MODEL / _KEY        |          | judge model name (required with the URL) / key  |
| CAPTAIN_EVAL_RUBRIC               |          | rubric file (policies/evaluation/rubric.json)    |
| CAPTAIN_EVAL_SAMPLE_RATE          |          | share of Tier 1-2 calls judged, default 0.05    |
| CAPTAIN_SECRETS_KEY               |          | AI keys entered on screen: hex:<64 hex> (dev) or |
|                                   |          | azurekv:<Key Vault RSA key URL>; unset = off     |
| CAPTAIN_EVAL_RATES                |          | JSON {"<register_id>": rate} per-entry overrides|
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from sqlalchemy import create_engine

from captain.core.alerts import EmailNotifier, LogNotifier, Notifier, Severity
from captain.core.evaluation.judge import HttpJudgeClient, Rubric
from captain.core.evaluation.service import DEFAULT_SAMPLE_RATE
from captain.core.factory import BuiltCore, CoreInfra, build_core
from captain.core.guardrails.detectors import Detector, RegexPiiDetector
from captain.core.guardrails.guard import HttpGuardClient
from captain.core.identity.oidc import HttpJwks, IssuerConfig, WorkloadAuthenticator
from captain.core.ledger.chain import KeyRingVerifier, PublicKey, Signer, load_public_key_pem
from captain.core.ledger.service import DirectoryWormStore, WormStore
from captain.core.ledger.signers import AzureKeyVaultSigner, PemFileSigner, parse_key_url
from captain.core.ledger.worm_cloud import AzureImmutableBlobWormStore, S3ObjectLockWormStore
from captain.core.models import ApprovalTicket, GovernanceRequest
from captain.core.policy.opa import OpaPolicyEngine
from captain.core.registry.service import RegistryService
from captain.core.secrets_store import SecretsService, build_wrapper
from captain.core.servicenow import ServiceNowNotifier
from captain.db.bus import PostgresBus
from captain.db.registry_store import PgRegistryStore
from captain.db.scrub import PgScrubber
from captain.db.stores import (
    PgAlertStore,
    PgApprovalStore,
    PgBudgetCatalog,
    PgBudgetStore,
    PgContextStore,
    PgIdentityStore,
    PgKillSwitchStore,
    PgLedgerStore,
    PgRegisterStore,
    PgSecretStore,
    PgSubjectStore,
    PgWorkloadBindings,
    load_price_catalog,
)
from captain.envfile import DEFAULT_ENV_FILE, dotenv_values


class ConfigError(ValueError):
    pass


# The reviewed rubric in this repository; containers set CAPTAIN_EVAL_RUBRIC.
DEFAULT_RUBRIC = Path(__file__).resolve().parents[2] / "policies" / "evaluation" / "rubric.json"


@dataclass(frozen=True)
class Settings:
    database_url: str
    pepper: bytes
    opa_url: str
    ledger_key_path: Path
    ledger_key_spec: str  # pem:<path> | azurekv:<key URL>
    ledger_key_id: str
    public_keys_dir: Path | None
    worm: str  # dir:<path> | s3:<bucket>[/<prefix>] | azure:<account_url>|<container>
    worm_retention_days: int
    require_budget: bool
    guard_urls: tuple[str, ...]
    guard_fallback: str | None
    presidio: bool
    approvers: dict[str, tuple[str, ...]]
    escalation: tuple[str, ...]
    oidc_issuers: tuple[IssuerConfig, ...]
    smtp: dict[str, str]
    alert_recipients: tuple[str, ...]
    servicenow: dict[str, str]
    judge_url: str | None = None
    judge_model: str | None = None
    judge_key: str | None = None
    eval_rubric: Path = DEFAULT_RUBRIC
    eval_sample_rate: float = DEFAULT_SAMPLE_RATE
    eval_rates: dict[str, float] = field(default_factory=dict)
    secrets_key: str | None = None  # CAPTAIN_SECRETS_KEY


def _csv(v: str | None) -> tuple[str, ...]:
    return tuple(x.strip() for x in (v or "").split(",") if x.strip())


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    if env is None:
        env = {**dotenv_values(DEFAULT_ENV_FILE), **os.environ}  # real env wins over .env

    def need(k: str) -> str:
        v = env.get(k)
        if not v:
            raise ConfigError(
                f"{k} is required (set it in the environment or in backend/.env; "
                "for local development run: python -m captain.devsetup)"
            )
        return v

    raw_pepper = need("CAPTAIN_KEY_PEPPER")  # outside the try: a missing value is not "bad hex"
    try:
        pepper = bytes.fromhex(raw_pepper)
    except ValueError as e:
        raise ConfigError("CAPTAIN_KEY_PEPPER must be hex") from e
    if len(pepper) < 32:
        raise ConfigError("CAPTAIN_KEY_PEPPER must be at least 32 bytes")
    signer = need("CAPTAIN_LEDGER_KEY")
    if not signer.startswith(("pem:", "azurekv:")):
        raise ConfigError(
            "CAPTAIN_LEDGER_KEY must be pem:<path> (development) or azurekv:<Key Vault key URL>"
        )
    if signer.startswith("azurekv:"):
        try:
            parse_key_url(signer[len("azurekv:") :])
        except ValueError as e:
            raise ConfigError(f"CAPTAIN_LEDGER_KEY: {e}") from e
    approvers_raw = json.loads(env.get("CAPTAIN_APPROVERS", "{}"))
    issuers = json.loads(env.get("CAPTAIN_OIDC_ISSUERS", "[]"))
    pk_dir = env.get("CAPTAIN_LEDGER_PUBLIC_KEYS_DIR")
    return Settings(
        database_url=need("CAPTAIN_DATABASE_URL"),
        pepper=pepper,
        opa_url=env.get("CAPTAIN_OPA_URL", "http://127.0.0.1:8181"),
        ledger_key_path=Path(signer[4:]) if signer.startswith("pem:") else Path(),
        ledger_key_spec=signer,
        ledger_key_id=env.get("CAPTAIN_LEDGER_KEY_ID", "ledger-1"),
        public_keys_dir=Path(pk_dir) if pk_dir else None,
        worm=env.get("CAPTAIN_WORM")
        or (
            f"dir:{env['CAPTAIN_WORM_DIR']}"
            if env.get("CAPTAIN_WORM_DIR")
            else need("CAPTAIN_WORM")
        ),
        worm_retention_days=int(env.get("CAPTAIN_WORM_RETENTION_DAYS", "400")),
        require_budget=env.get("CAPTAIN_REQUIRE_BUDGET", "1") != "0",
        guard_urls=_csv(env.get("CAPTAIN_GUARD_URLS")),
        guard_fallback=env.get("CAPTAIN_GUARD_FALLBACK") or None,
        presidio=env.get("CAPTAIN_PRESIDIO") == "1",
        approvers={k: tuple(v) for k, v in approvers_raw.items()},
        escalation=_csv(env.get("CAPTAIN_ESCALATION_APPROVERS")),
        oidc_issuers=tuple(
            IssuerConfig(i["issuer"], i["audience"], i["jwks_uri"]) for i in issuers
        ),
        smtp={
            k: env[f"CAPTAIN_SMTP_{k}"]
            for k in ("HOST", "PORT", "FROM", "USER", "PASSWORD")
            if env.get(f"CAPTAIN_SMTP_{k}")
        },  # fmt: skip
        alert_recipients=_csv(env.get("CAPTAIN_ALERT_RECIPIENTS")),
        servicenow={
            k: env[f"CAPTAIN_SERVICENOW_{k}"]
            for k in ("URL", "USER", "PASSWORD", "MIN_SEVERITY", "GROUP")
            if env.get(f"CAPTAIN_SERVICENOW_{k}")
        },
        **_eval_settings(env),
        secrets_key=env.get("CAPTAIN_SECRETS_KEY") or None,
    )


class _EvalSettings(TypedDict):
    judge_url: str | None
    judge_model: str | None
    judge_key: str | None
    eval_rubric: Path
    eval_sample_rate: float
    eval_rates: dict[str, float]


def _eval_settings(env: Mapping[str, str]) -> _EvalSettings:
    url = env.get("CAPTAIN_JUDGE_URL") or None
    model = env.get("CAPTAIN_JUDGE_MODEL") or None
    if url and not model:
        raise ConfigError("CAPTAIN_JUDGE_MODEL is required when CAPTAIN_JUDGE_URL is set")
    rate = float(env.get("CAPTAIN_EVAL_SAMPLE_RATE", str(DEFAULT_SAMPLE_RATE)))
    rates = {str(k): float(v) for k, v in json.loads(env.get("CAPTAIN_EVAL_RATES", "{}")).items()}
    if not all(0 <= r <= 1 for r in (rate, *rates.values())):
        raise ConfigError("evaluation sample rates must be between 0 and 1")
    rubric = env.get("CAPTAIN_EVAL_RUBRIC")
    return {
        "judge_url": url,
        "judge_model": model,
        "judge_key": env.get("CAPTAIN_JUDGE_KEY") or None,
        "eval_rubric": Path(rubric) if rubric else DEFAULT_RUBRIC,
        "eval_sample_rate": rate,
        "eval_rates": rates,
    }


def _pii(presidio: bool) -> Detector | None:
    if not presidio:
        return None
    from presidio_analyzer import AnalyzerEngine  # optional extra

    from captain.core.guardrails.presidio import CombinedDetector, PresidioDetector

    return CombinedDetector(RegexPiiDetector(), PresidioDetector(AnalyzerEngine()))


def build_worm(spec: str, retention_days: int) -> WormStore:
    """Write-once anchor storage. Cloud stores are checked at startup: Captain refuses
    to run if the bucket/container is not set up for write-once."""
    kind, _, rest = spec.partition(":")
    if kind == "dir":
        return DirectoryWormStore(Path(rest))
    if kind == "s3":
        import boto3  # optional extra: pip install -e ".[aws]"

        bucket, _, prefix = rest.partition("/")
        s3 = S3ObjectLockWormStore(
            boto3.client("s3"),
            bucket,
            (prefix.rstrip("/") + "/") if prefix else "captain/anchors/",
            retention_days,
        )
        s3.verify_configuration()
        return s3
    if kind == "azure":
        from azure.identity import DefaultAzureCredential  # optional extra: ".[azure]"
        from azure.storage.blob import ContainerClient

        account_url, _, container = rest.partition("|")
        az = AzureImmutableBlobWormStore(
            ContainerClient(account_url, container, credential=DefaultAzureCredential())
        )
        az.verify_configuration()
        return az
    raise ConfigError(f"CAPTAIN_WORM must start with dir:, s3: or azure: (got {kind!r})")


def build_from_settings(s: Settings) -> BuiltCore:
    engine = create_engine(s.database_url, pool_size=20, max_overflow=20, pool_pre_ping=True)
    signer: Signer
    if s.ledger_key_spec.startswith("azurekv:"):
        signer, public = AzureKeyVaultSigner.from_key_url(s.ledger_key_spec[len("azurekv:") :])
    else:
        pem = PemFileSigner(s.ledger_key_id, s.ledger_key_path)
        signer, public = pem, pem.public_key()
    keys: dict[str, PublicKey] = {signer.key_id: public}
    if s.public_keys_dir:
        for p in s.public_keys_dir.glob("*.pem"):
            keys.setdefault(p.stem, load_public_key_pem(p.read_bytes()))
    notifiers: list[Notifier] = [LogNotifier()]
    if "HOST" in s.smtp:
        notifiers.append(EmailNotifier(
            s.smtp["HOST"], int(s.smtp.get("PORT", "587")), s.smtp.get("FROM", "captain@localhost"),
            s.smtp.get("USER"), s.smtp.get("PASSWORD"),
        ))  # fmt: skip
    if "URL" in s.servicenow:
        notifiers.append(
            ServiceNowNotifier(
                s.servicenow["URL"],
                s.servicenow.get("USER", ""),
                s.servicenow.get("PASSWORD", ""),
                Severity(s.servicenow.get("MIN_SEVERITY", "CRITICAL")),
                s.servicenow.get("GROUP"),
            )
        )
    issuers = {i.issuer: i for i in s.oidc_issuers}

    def approvers(req: GovernanceRequest) -> tuple[str, ...]:
        return s.approvers.get(req.context.register_entry.id) or s.approvers.get("*", ())

    return build_core(
        CoreInfra(
            pepper=s.pepper,
            policy=OpaPolicyEngine(s.opa_url),
            guard=HttpGuardClient(list(s.guard_urls), s.guard_fallback) if s.guard_urls else None,
            signer=signer,
            verifier=KeyRingVerifier(keys),
            approvers=approvers,
            bus=PostgresBus(s.database_url),
            identity_store=PgIdentityStore(engine),
            context_store=PgContextStore(engine),
            register_store=PgRegisterStore(engine),
            killswitch_store=PgKillSwitchStore(engine),
            budget_catalog=PgBudgetCatalog(engine),
            budget_store=PgBudgetStore(engine),
            approval_store=PgApprovalStore(engine),
            ledger_store=PgLedgerStore(engine),
            worm=build_worm(s.worm, s.worm_retention_days),
            workload=WorkloadAuthenticator(issuers, HttpJwks(issuers), PgWorkloadBindings(engine))
            if issuers
            else None,
            pii=_pii(s.presidio),
            notifiers=tuple(notifiers),
            alert_store=PgAlertStore(engine),
            registry=RegistryService(PgRegistryStore(engine)),
            require_budget=s.require_budget,
            prices=load_price_catalog(engine),
            alert_recipients=lambda a: s.alert_recipients,
            judge=HttpJudgeClient(s.judge_url, s.judge_model or "", s.judge_key)
            if s.judge_url
            else None,
            rubric=Rubric.load(s.eval_rubric) if s.judge_url else None,
            eval_default_rate=s.eval_sample_rate,
            eval_rates=s.eval_rates,
            subject_store=PgSubjectStore(engine),
            scrubber=PgScrubber(engine),
        )
    )


def build_from_env() -> tuple[BuiltCore, Settings]:
    s = load_settings()
    return build_from_settings(s), s


def escalation_approvers_from_env() -> Callable[[ApprovalTicket], tuple[str, ...]]:
    env = {**dotenv_values(DEFAULT_ENV_FILE), **os.environ}
    backup = _csv(env.get("CAPTAIN_ESCALATION_APPROVERS"))

    def resolve(_: ApprovalTicket) -> tuple[str, ...]:
        return backup

    return resolve


def build_secrets(s: Settings, engine: Any) -> SecretsService | None:
    """Company AI keys stored encrypted in Captain (plan step 3); None when not configured."""
    if not s.secrets_key:
        return None
    try:
        wrapper = build_wrapper(s.secrets_key)
    except ValueError as e:
        raise ConfigError(f"CAPTAIN_SECRETS_KEY: {e}") from e
    return SecretsService(PgSecretStore(engine), wrapper)
