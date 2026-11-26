from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from captain.db.schema import metadata
from captain.envfile import current_env

url = current_env().get("CAPTAIN_DATABASE_URL")
if not url:
    raise SystemExit(
        "Set CAPTAIN_DATABASE_URL, e.g. postgresql+psycopg://captain@localhost:5432/captain"
    )

engine = create_engine(url)
with engine.connect() as conn:
    context.configure(connection=conn, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()
