"""4.5 — why a Discover row was hidden (owner, 2026-09-27).

A nullable `hidden_reason` on discovery_items: not_my_thing | closed |
been_there. Hiding defaults to not_my_thing, so agents can read hidden rows as
the person's dislikes. Existing hidden rows get not_my_thing.

Revision ID: 0031
Revises: 0030
"""
import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = sa.inspect(op.get_bind()).get_columns("discovery_items")
    if not any(c["name"] == "hidden_reason" for c in cols):
        op.add_column("discovery_items", sa.Column("hidden_reason", sa.String(16)))
    items = sa.table("discovery_items", sa.column("hidden", sa.Boolean),
                     sa.column("hidden_reason", sa.String))
    op.execute(items.update().where(items.c.hidden.is_(True))
               .where(items.c.hidden_reason.is_(None))
               .values(hidden_reason="not_my_thing"))


def downgrade() -> None:
    op.drop_column("discovery_items", "hidden_reason")
