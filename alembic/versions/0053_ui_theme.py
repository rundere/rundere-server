"""Run·dere — travel theme and scenery per account (SPEC-8 §15, D-237, D-239).

`users.ui_theme` is the person's theme id, a plain string checked against the
generated manifest (design/themes/themes.json) by the API, never a database
enum: new theme packages need no migration (D-240). `users.scenery` turns the
scenery floor on or off. Existing accounts get Base with scenery on.

Revision ID: 0053
Revises: 0052
"""
import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "users" not in sa.inspect(bind).get_table_names():
        return
    have = {c["name"] for c in sa.inspect(bind).get_columns("users")}
    with op.batch_alter_table("users") as b:
        if "ui_theme" not in have:
            b.add_column(sa.Column("ui_theme", sa.String(32), nullable=False,
                                   server_default="base"))
        if "scenery" not in have:
            b.add_column(sa.Column("scenery", sa.Boolean(), nullable=False,
                                   server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.drop_column("scenery")
        b.drop_column("ui_theme")
