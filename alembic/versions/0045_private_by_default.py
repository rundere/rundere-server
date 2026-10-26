"""Run·dere — new accounts start private (D-220, YONDER-335).

`users.share_with_admins` now defaults to off. Only the column's server
default changes: every existing account keeps the choice it has.

Revision ID: 0045
Revises: 0044
"""
import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.alter_column("share_with_admins", existing_type=sa.Boolean(),
                           existing_nullable=False, server_default=sa.false())


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.alter_column("share_with_admins", existing_type=sa.Boolean(),
                           existing_nullable=False, server_default=sa.true())
