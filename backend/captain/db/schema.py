"""PostgreSQL schema for Captain Core (SQLAlchemy Core tables).

Mutable configuration tables vs append-only event tables are kept separate.
Every table carries org_id where it makes sense, so per-customer isolation stays
possible even though each customer gets its own deployment.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

TS = DateTime(timezone=True)

# ------------------------------------------------------------- identity (C1)
principals = Table(
    "principals", metadata,
    Column("id", String(128), primary_key=True),
    Column("type", String(32), nullable=False),
    Column("org_id", String(128), nullable=False, index=True),
    Column("team_id", String(128)),
    Column("display_name", String(256), nullable=False),
    Column("idp_subject", String(256), unique=True),
    Column("roles", ARRAY(String(64)), nullable=False, server_default="{}"),
    Column("active", Boolean, nullable=False, server_default="true"),
)  # fmt: skip

virtual_keys = Table(
    "virtual_keys", metadata,
    Column("key_id", String(16), primary_key=True),
    Column("secret_hash", String(64), nullable=False),  # HMAC-SHA-256; raw key never stored
    Column("principal_id", String(128), ForeignKey("principals.id"), nullable=False, index=True),
    Column("register_id", String(128), nullable=False, index=True),
    Column("project_id", String(128), nullable=False),
    Column("application_id", String(128), nullable=False),
    Column("environment", String(16), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("expires_at", TS),
    Column("status", String(16), nullable=False),
    Column("revoked_at", TS),
    Column("revoked_reason", Text),
    Column("rotated_from", String(16)),
    Column("last_used_at", TS),
    Column("allowed_cidrs", ARRAY(String(64)), nullable=False, server_default="{}"),
    Column("label", String(256), nullable=False, server_default=""),
)  # fmt: skip

kill_switches = Table(
    "kill_switches", metadata,
    Column("scope", String(32), primary_key=True),
    Column("target", String(256), primary_key=True),
    Column("reason", Text, nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("activated_at", TS, nullable=False),
)  # fmt: skip

# -------------------------------------------------------------- context (C2)
register_entries = Table(
    "register_entries", metadata,
    Column("id", String(128), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("doc", JSONB, nullable=False),  # RegisterEntry as JSON (Register is source of truth)
    Column("updated_at", TS, nullable=False),
)  # fmt: skip

projects = Table(
    "projects", metadata,
    Column("id", String(128), primary_key=True),
    Column("org_id", String(128), nullable=False, index=True),
    Column("name", String(256), nullable=False),
    Column("team_id", String(128)),
    Column("active", Boolean, nullable=False, server_default="true"),
)  # fmt: skip

applications = Table(
    "applications", metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), ForeignKey("projects.id"), nullable=False, index=True),
    Column("register_id", String(128), nullable=False),
    Column("name", String(256), nullable=False),
    Column("environments", ARRAY(String(16)), nullable=False),
    Column("active", Boolean, nullable=False, server_default="true"),
)  # fmt: skip

# --------------------------------------------------------------- budget (C5)
budget_definitions = Table(
    "budget_definitions", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("scope", String(256), nullable=False, index=True),
    Column("period", String(16), nullable=False),
    Column("limit_micros", BigInteger, nullable=False),
    Column("hard", Boolean, nullable=False),
    Column("alert_thresholds", ARRAY(String(8)), nullable=False),
    UniqueConstraint("scope", "period"),
)  # fmt: skip

rate_limits = Table(
    "rate_limits", metadata,
    Column("scope", String(256), primary_key=True),
    Column("requests_per_minute", Integer),
    Column("tokens_per_minute", Integer),
)  # fmt: skip

budget_buckets = Table(
    "budget_buckets", metadata,
    Column("bucket", String(320), primary_key=True),
    Column("limit_micros", BigInteger, nullable=False),
    Column("spent_micros", BigInteger, nullable=False, server_default="0"),
    Column("reserved_micros", BigInteger, nullable=False, server_default="0"),
    Column("extra_micros", BigInteger, nullable=False, server_default="0"),  # overrides (rev 0005)
)  # fmt: skip

budget_reservations = Table(
    "budget_reservations", metadata,
    Column("reservation_id", String(64), primary_key=True),
    Column("amount_micros", BigInteger, nullable=False),
    Column("buckets", ARRAY(String(320)), nullable=False),
    Column("expires_at", TS, nullable=False, index=True),
)  # fmt: skip

rate_windows = Table(
    "rate_windows", metadata,
    Column("bucket", String(320), primary_key=True),
    Column("minute", BigInteger, primary_key=True),
    Column("requests", Integer, nullable=False),
    Column("tokens", BigInteger, nullable=False),
)  # fmt: skip

# ------------------------------------------------------------ approvals (C6)
approval_tickets = Table(
    "approval_tickets", metadata,
    Column("ticket_id", String(36), primary_key=True),
    Column("state", String(16), nullable=False, index=True),
    Column("requested_by", String(128), nullable=False, index=True),
    Column("doc", JSONB, nullable=False),  # full ApprovalTicket (no raw arguments)
    Column("updated_at", TS, nullable=False),
)  # fmt: skip

# ---------------------------------------------------------------- pricing (C7)
model_prices = Table(
    "model_prices", metadata,
    Column("model", String(128), primary_key=True),
    Column("version", String(64), primary_key=True),
    Column("provider", String(64), nullable=False),
    Column("effective_from", TS, nullable=False),
    Column("input_per_m", BigInteger, nullable=False),
    Column("output_per_m", BigInteger, nullable=False),
    Column("cached_input_per_m", BigInteger),
    Column("cache_write_per_m", BigInteger),
    Column("currency", String(3), nullable=False),
)  # fmt: skip

# ---------------------------------------------------------------- ledger (C8)
ledger_outbox = Table(
    "ledger_outbox", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),  # arrival order
    Column("event_id", String(36), nullable=False, unique=True),
    Column("partition", String(128), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("body", JSONB, nullable=False),
)  # fmt: skip

evidence_ledger = Table(
    "evidence_ledger", metadata,
    Column("partition", String(128), primary_key=True),
    Column("seq", BigInteger, primary_key=True),
    Column("event_id", String(36), nullable=False, unique=True),
    Column("event_type", String(64), nullable=False),
    Column("recorded_at", TS, nullable=False),
    Column("body", JSONB, nullable=False),
    Column("prev_hash", String(64), nullable=False),
    Column("entry_hash", String(64), nullable=False),
    Column("signature", Text, nullable=False),
    Column("key_id", String(64), nullable=False),
    Index("ix_evidence_ledger_recorded_at", "recorded_at"),
)  # fmt: skip

# Anchor membership lives outside the append-only ledger so the ledger never needs UPDATE.
ledger_anchor_ranges = Table(
    "ledger_anchor_ranges", metadata,
    Column("anchor_id", String(64), primary_key=True),
    Column("partition", String(128), primary_key=True),
    Column("first_seq", BigInteger, nullable=False),
    Column("last_seq", BigInteger, nullable=False),
    Column("created_at", TS, nullable=False),
    Index("ix_anchor_partition_last", "partition", "last_seq"),
)  # fmt: skip

# ------------------------------------------------ workload identity (C1, rev 0002)
workload_bindings = Table(
    "workload_bindings",
    metadata,
    Column("issuer", String(512), primary_key=True),
    Column("subject", String(512), primary_key=True),
    Column("principal_id", String(128), ForeignKey("principals.id"), nullable=False),
    Column("register_id", String(128), nullable=False),
    Column("project_id", String(128), nullable=False),
    Column("application_id", String(128), nullable=False),
    Column("environment", String(16), nullable=False),
)

# ----------------------------------------------------------- alerts (rev 0003)
alerts = Table(
    "alerts", metadata,
    Column("id", String(36), primary_key=True),
    Column("kind", String(64), nullable=False, index=True),
    Column("severity", String(16), nullable=False),
    Column("created_at", TS, nullable=False, index=True),
    Column("delivered", Boolean, nullable=False, server_default="false"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("doc", JSONB, nullable=False),
)  # fmt: skip

# ---------------------------------------------- HTTP API server state (rev 0007)
api_state = Table(
    "api_state", metadata,
    Column("kind", String(16), primary_key=True),  # decision | reservation
    Column("key", String(64), primary_key=True),
    Column("owner", String(128), nullable=False),
    Column("doc", JSONB, nullable=False),  # never contains prompt content
    Column("expires_at", TS, nullable=False, index=True),
)  # fmt: skip

# ------------------------------------------------- budget alert marks (rev 0006)
budget_alert_marks = Table(
    "budget_alert_marks", metadata,
    Column("bucket", String(320), primary_key=True),  # bucket name includes the period
    Column("threshold", String(8), primary_key=True),
    Column("at", TS, nullable=False),
)  # fmt: skip

# ------------------------------------------------- model/tool registry (rev 0004)
registry_items = Table(
    "registry_items", metadata,
    Column("kind", String(16), primary_key=True),  # provider | model | route | server | tool
    Column("key", String(256), primary_key=True),
    Column("doc", JSONB, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("updated_at", TS, nullable=False),
)  # fmt: skip

# Erasure keys (ADR-005). Deleting a row is the erasure: kept OUT of the ledger, and
# never append-only. Rows are a person ("<principal id>") or a client-IP day ("ipday:<date>").
subject_keys = Table(
    "subject_keys", metadata,
    Column("subject", String(256), primary_key=True),
    Column("key", LargeBinary, nullable=False),
    Column("pseudonym", String(64), nullable=False, unique=True),
    Column("created_at", TS, nullable=False),
)  # fmt: skip

APPEND_ONLY_TABLES = ("evidence_ledger",)

# Tables created by later migrations. Revision 0001 must not create them.
TABLES_SINCE = {
    "0002": ("workload_bindings",),
    "0003": ("alerts",),
    "0004": ("registry_items",),
    "0006": ("budget_alert_marks",),
    "0007": ("api_state",),
    "0009": ("subject_keys",),
}
