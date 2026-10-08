"""langgraph checkpoint tables

Revision ID: 202610070008
Revises: 202607010007
Create Date: 2026-10-07

Applies the LangGraph Postgres checkpoint schema (checkpoints, checkpoint_blobs,
checkpoint_writes + their indexes) as an Alembic migration, replicating exactly
what ``AsyncPostgresSaver.setup()`` does at runtime — including the
``checkpoint_migrations`` bookkeeping inserts — so:

- the runtime no longer needs DDL privileges just to checkpoint searches;
- an explicit runtime ``setup()`` (if ever requested) sees every version
  recorded and becomes a no-op;
- databases bootstrapped earlier by the runtime ``setup()`` are recognized: the
  bookkeeping table records the applied versions and this migration skips them.

The DDL is imported from the installed ``langgraph-checkpoint-postgres`` package
(the ``MIGRATIONS`` list in ``langgraph.checkpoint.postgres.base``), so it always
matches the library version pinned in ``pyproject.toml`` / ``uv.lock`` — bump the
pin and this migration picks the new statements up on the next fresh database.
``CREATE INDEX CONCURRENTLY`` cannot run inside the migration transaction and
goes through an autocommit block.
"""

from collections.abc import Sequence

from langgraph.checkpoint.postgres.base import MIGRATIONS
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202610070008"
down_revision: str | None = "202607010007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECORD_VERSION = text("INSERT INTO checkpoint_migrations (v) VALUES (:v)")


def _applied_version() -> int:
    result = op.get_bind().execute(
        text("SELECT v FROM checkpoint_migrations ORDER BY v DESC LIMIT 1")
    )
    row = result.first()
    return -1 if row is None else int(row[0])


def upgrade() -> None:
    # MIGRATIONS[0] creates the bookkeeping table (IF NOT EXISTS), so databases
    # previously bootstrapped by the runtime setup() stay compatible.
    op.get_bind().exec_driver_sql(MIGRATIONS[0])
    for version in range(_applied_version() + 1, len(MIGRATIONS)):
        statement = MIGRATIONS[version]
        if "CREATE INDEX CONCURRENTLY" in statement:
            with op.get_context().autocommit_block():
                op.get_bind().exec_driver_sql(statement)
                op.get_bind().execute(_RECORD_VERSION, {"v": version})
        else:
            op.get_bind().exec_driver_sql(statement)
            op.get_bind().execute(_RECORD_VERSION, {"v": version})


def downgrade() -> None:
    # Checkpoint history is derived data (new searches recreate it), so dropping
    # the tables is the honest reverse of this migration.
    op.get_bind().exec_driver_sql(
        "DROP TABLE IF EXISTS checkpoint_writes, checkpoint_blobs, checkpoints,"
        " checkpoint_migrations"
    )
