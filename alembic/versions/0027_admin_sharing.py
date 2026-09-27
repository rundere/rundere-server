"""Yonder 4.1 — users can opt out of sharing their data with admins.

`users.share_with_admins` (default TRUE, so every existing account keeps today's
behaviour: admins see and edit everything) and `users.sharing_prompt` (ask a
self-registered/SSO user to choose at first sign-in). `account_events` is an
append-only audit trail of admin actions on accounts and sharing changes.

Revision ID: 0027
Revises: 0026
"""
import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def _cols(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    cols = _cols("users")
    if "share_with_admins" not in cols:
        op.add_column("users", sa.Column("share_with_admins", sa.Boolean(),
                                         nullable=False, server_default=sa.true()))
    if "sharing_prompt" not in cols:
        op.add_column("users", sa.Column("sharing_prompt", sa.Boolean(),
                                         nullable=False, server_default=sa.false()))
    if "account_events" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "account_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), nullable=False, index=True),
            sa.Column("actor_id", sa.String(36), nullable=True),
            sa.Column("action", sa.String(32), nullable=False),
            sa.Column("detail", sa.String(255), nullable=True),
            sa.Column("at", sa.DateTime(timezone=True), nullable=False, index=True),
        )


def downgrade() -> None:
    op.drop_table("account_events")
    op.drop_column("users", "sharing_prompt")
    op.drop_column("users", "share_with_admins")
