"""Run·dere — support access replaces admin sharing (D-211, YONDER-336).

`share_with_admins` now means "let admins view my trips and Inbox, read-only".
It gains an end time (`support_until`, NULL = until turned off) and a one-time
notice flag. Everyone sharing today keeps a standing grant and is told once
that it is now read-only.

Revision ID: 0046
Revises: 0045
"""
import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    with op.batch_alter_table("users") as batch:
        if "support_until" not in cols:
            batch.add_column(sa.Column("support_until", sa.DateTime(timezone=True)))
        if "support_notice" not in cols:
            batch.add_column(sa.Column("support_notice", sa.Boolean(), nullable=False,
                                       server_default=sa.false()))
    users = sa.table("users", sa.column("share_with_admins", sa.Boolean),
                     sa.column("support_notice", sa.Boolean))
    op.get_bind().execute(sa.update(users).where(users.c.share_with_admins.is_(True))
                          .values(support_notice=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("support_notice")
        batch.drop_column("support_until")
