"""Add improved status to tables (is_active -> status)

Revision ID: e75490e949b1
Revises: e4fc04d1a442
Create Date: 2025-07-02 17:12:40.678256
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import Connection

# revision identifiers, used by Alembic.
revision: str = 'e75490e949b1'
down_revision: Union[str, Sequence[str], None] = 'e4fc04d1a442'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    conn: Connection = op.get_bind()
    dialect = conn.dialect.name

    # Add status column as nullable first
    with op.batch_alter_table('tools') as batch_op:
        batch_op.add_column(sa.Column('status', sa.JSON(), nullable=True))

    with op.batch_alter_table('gateways') as batch_op:
        batch_op.add_column(sa.Column('status', sa.JSON(), nullable=True))

    # Populate status from is_active
    if dialect == 'postgresql':
        op.execute("""
            UPDATE tools
            SET status = jsonb_build_object('enabled', is_active, 'reachable', true)
        """)
        op.execute("""
            UPDATE gateways
            SET status = jsonb_build_object('enabled', is_active, 'reachable', true)
        """)
    else:
        op.execute("""
            UPDATE tools
            SET status = json_object('enabled', is_active, 'reachable', 1)
        """)
        op.execute("""
            UPDATE gateways
            SET status = json_object('enabled', is_active, 'reachable', 1)
        """)

    # Use batch_alter_table to make status column non-nullable and drop is_active
    with op.batch_alter_table('tools') as batch_op:
        batch_op.alter_column('status', nullable=False)
        batch_op.drop_column('is_active')

    with op.batch_alter_table('gateways') as batch_op:
        batch_op.alter_column('status', nullable=False)
        batch_op.drop_column('is_active')


def downgrade():
    conn: Connection = op.get_bind()
    dialect = conn.dialect.name

    # Re-add is_active column
    with op.batch_alter_table('tools') as batch_op:
        batch_op.add_column(sa.Column('is_active', sa.Boolean(), nullable=True))

    with op.batch_alter_table('gateways') as batch_op:
        batch_op.add_column(sa.Column('is_active', sa.Boolean(), nullable=True))

    # Restore is_active from status
    if dialect == 'postgresql':
        op.execute("""
            UPDATE tools
            SET is_active = (status ->> 'enabled')::boolean
        """)
        op.execute("""
            UPDATE gateways
            SET is_active = (status ->> 'enabled')::boolean
        """)
    else:
        op.execute("""
            UPDATE tools
            SET is_active = CAST(json_extract(status, '$.enabled') AS BOOLEAN)
        """)
        op.execute("""
            UPDATE gateways
            SET is_active = CAST(json_extract(status, '$.enabled') AS BOOLEAN)
        """)

    # Use batch_alter_table to set is_active non-nullable and drop status
    with op.batch_alter_table('tools') as batch_op:
        batch_op.alter_column('is_active', nullable=False)
        batch_op.drop_column('status')

    with op.batch_alter_table('gateways') as batch_op:
        batch_op.alter_column('is_active', nullable=False)
        batch_op.drop_column('status')
