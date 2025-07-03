"""Add structured status column and migrate data (is_active ➜ status).

Revision ID: e75490e949b1
Revises: e4fc04d1a442
Create Date: 2025‑07‑02 17:12:40.678256
"""

# Standard
from typing import Sequence, Union

# Alembic / SQLAlchemy
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.types import TypeEngine

# Revision identifiers.
revision: str = "e75490e949b1"
down_revision: Union[str, Sequence[str], None] = "e4fc04d1a442"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _status_type(dialect_name: str) -> TypeEngine:
    """Return the correct JSON/JSONB type for the current dialect."""
    if dialect_name == "postgresql":
        from sqlalchemy.dialects import postgresql as pg

        return pg.JSONB()
    return sa.JSON()


def upgrade() -> None:
    conn: Connection = op.get_bind()
    dialect = conn.dialect.name
    status_type = _status_type(dialect)

    # 1. Add nullable `status` column
    for tbl in ("tools", "gateways"):
        with op.batch_alter_table(tbl) as batch_op:
            batch_op.add_column(sa.Column("status", status_type, nullable=True))

    # 2. Copy data from `is_active` ➜ `status`
    if dialect == "postgresql":
        # jsonb primitive true/false stays boolean-ish but still needs ->> casting later
        op.execute(
            """
            UPDATE tools
               SET status = jsonb_build_object('enabled', is_active, 'reachable', true)
            """
        )
        op.execute(
            """
            UPDATE gateways
               SET status = jsonb_build_object('enabled', is_active, 'reachable', true)
            """
        )
    else:
        # MySQL/SQLite JSON uses 1/0 for booleans, ensure numeric comparison
        op.execute(
            """
            UPDATE tools
               SET status = json_object('enabled', CASE WHEN is_active = 1 THEN 1 ELSE 0 END,
                                         'reachable', 1)
            """
        )
        op.execute(
            """
            UPDATE gateways
               SET status = json_object('enabled', CASE WHEN is_active = 1 THEN 1 ELSE 0 END,
                                         'reachable', 1)
            """
        )

    # 3. Make `status` non‑nullable and drop `is_active`
    for tbl in ("tools", "gateways"):
        with op.batch_alter_table(tbl) as batch_op:
            batch_op.alter_column("status", nullable=False)
            batch_op.drop_column("is_active")

    # 4. Helpful functional indexes for Postgres
    if dialect == "postgresql":
        op.execute(
            "CREATE INDEX idx_tools_enabled_bool ON tools ((status ->> 'enabled'))"
        )
        op.execute(
            "CREATE INDEX idx_gateways_enabled_bool ON gateways ((status ->> 'enabled'))"
        )


def downgrade() -> None:
    conn: Connection = op.get_bind()
    dialect = conn.dialect.name
    status_type = _status_type(dialect)

    # 1. Re‑add nullable `is_active`
    for tbl in ("tools", "gateways"):
        with op.batch_alter_table(tbl) as batch_op:
            batch_op.add_column(sa.Column("is_active", sa.Boolean(), nullable=True))

    # 2. Copy data back
    if dialect == "postgresql":
        op.execute(
            """
            UPDATE tools
               SET is_active = (status ->> 'enabled')::boolean
            """
        )
        op.execute(
            """
            UPDATE gateways
               SET is_active = (status ->> 'enabled')::boolean
            """
        )
    else:
        op.execute(
            """
            UPDATE tools
               SET is_active = CAST(json_extract(status, '$.enabled') AS BOOLEAN)
            """
        )
        op.execute(
            """
            UPDATE gateways
               SET is_active = CAST(json_extract(status, '$.enabled') AS BOOLEAN)
            """
        )

    # 3. Make `is_active` NOT NULL, drop `status`
    for tbl in ("tools", "gateways"):
        with op.batch_alter_table(tbl) as batch_op:
            batch_op.alter_column("is_active", nullable=False)
            batch_op.drop_column("status")

    # 4. Drop Postgres indexes if they exist
    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS idx_tools_enabled_bool")
        op.execute("DROP INDEX IF EXISTS idx_gateways_enabled_bool")
