"""Run·dere — server-wide settings an admin changes in the app (D-208,
YONDER-332): the first is whether the inbox folder is Run·dere's alone.

Revision ID: 0043
Revises: 0042
"""
import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "server_settings" not in have:
        op.create_table(
            "server_settings",
            sa.Column("key", sa.String(64), primary_key=True),
            sa.Column("value", sa.String(200), nullable=False, server_default=""),
            sa.Column("updated_by", sa.String(36)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    op.drop_table("server_settings")
