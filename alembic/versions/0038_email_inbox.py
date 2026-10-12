"""Run·dere 4.9 — the email-forward inbox (YONDER-199, SPEC-9 D-162, D-190):
each user's plus-address token, and the poller's record of handled messages.

Revision ID: 0038
Revises: 0037
"""
import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "inbox_addresses" not in have:
        op.create_table(
            "inbox_addresses",
            sa.Column("user_id", sa.String(36), primary_key=True),
            sa.Column("token", sa.String(32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_inbox_addresses_token", "inbox_addresses", ["token"], unique=True)
    if "inbox_messages" not in have:
        op.create_table(
            "inbox_messages",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("message_key", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(36)),
            sa.Column("outcome", sa.String(16), nullable=False),
            sa.Column("detail", sa.String(200)),
            sa.Column("batch_id", sa.String(36)),
            sa.Column("received_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_inbox_messages_message_key", "inbox_messages", ["message_key"],
                        unique=True)
        op.create_index("ix_inbox_messages_user_id", "inbox_messages", ["user_id"])
        op.create_index("ix_inbox_messages_outcome", "inbox_messages", ["outcome"])
        op.create_index("ix_inbox_messages_received_at", "inbox_messages", ["received_at"])


def downgrade() -> None:
    op.drop_table("inbox_messages")
    op.drop_table("inbox_addresses")
