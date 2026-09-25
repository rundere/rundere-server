"""Yonder 4.1 — Discover is per person.

A trip's travellers and timeline are shared, but Discover reflects the person
viewing: their tastes (discovery profile), nationality (visa), base currency
and homes. So discovery runs, findings and excluded places gain a `user_id`
(soft link, no FK — house convention), exactly like packing plans/items did in
0022. Existing rows are assigned to their trip's owner, so today's results stay
with the person who has them; on an auth-off deployment trips have no owner and
the rows stay NULL, which is what that single-user mode reads.

Revision ID: 0026
Revises: 0025
"""
import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_TABLES = ("discovery_runs", "discovery_findings", "discovery_exclusions")


def _cols(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for t in _TABLES:
        if "user_id" not in _cols(t):
            op.add_column(t, sa.Column("user_id", sa.String(36), nullable=True))
            op.create_index(f"ix_{t}_user_id", t, ["user_id"])
        op.execute(f"UPDATE {t} SET user_id = (SELECT owner_id FROM trips "
                   f"WHERE trips.id = {t}.trip_id) WHERE user_id IS NULL")


def downgrade() -> None:
    for t in _TABLES:
        op.drop_index(f"ix_{t}_user_id", table_name=t)
        op.drop_column(t, "user_id")
