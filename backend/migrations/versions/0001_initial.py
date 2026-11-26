"""Initial Captain Core schema + append-only evidence ledger.

Revision ID: 0001
Revises:
"""

from alembic import op

from captain.db.schema import TABLES_SINCE, metadata

_LATER = {name for names in TABLES_SINCE.values() for name in names}

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APPEND_ONLY_SQL = """
CREATE OR REPLACE FUNCTION captain_reject_ledger_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'evidence_ledger is append-only (% blocked)', TG_OP
    USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE TRIGGER evidence_ledger_no_update_delete
  BEFORE UPDATE OR DELETE ON evidence_ledger
  FOR EACH ROW EXECUTE FUNCTION captain_reject_ledger_mutation();

CREATE TRIGGER evidence_ledger_no_truncate
  BEFORE TRUNCATE ON evidence_ledger
  FOR EACH STATEMENT EXECUTE FUNCTION captain_reject_ledger_mutation();

-- Optional least-privilege runtime role. If a role named captain_app exists, it may
-- only INSERT/SELECT the ledger; the migrating (owner) role is the break-glass role.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'captain_app') THEN
    REVOKE ALL ON evidence_ledger FROM captain_app;
    GRANT SELECT, INSERT ON evidence_ledger TO captain_app;
  END IF;
END $$;
"""


def upgrade() -> None:
    bind = op.get_bind()
    metadata.create_all(bind, tables=[t for t in metadata.sorted_tables if t.name not in _LATER])
    op.execute(APPEND_ONLY_SQL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS evidence_ledger_no_truncate ON evidence_ledger")
    op.execute("DROP TRIGGER IF EXISTS evidence_ledger_no_update_delete ON evidence_ledger")
    op.execute("DROP FUNCTION IF EXISTS captain_reject_ledger_mutation()")
    metadata.drop_all(
        op.get_bind(), tables=[t for t in metadata.sorted_tables if t.name not in _LATER]
    )
