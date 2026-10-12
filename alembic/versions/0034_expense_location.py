"""Run·dere 4.8 — where an actual expense was logged (YONDER-212).

Nullable place columns on trip_expenses, named like the journal's. Older rows
and settlements keep them NULL; only the iOS app captures a location.

Revision ID: 0034
Revises: 0033
"""
import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

_COLUMNS = (("place_lat", sa.Float), ("place_lon", sa.Float),
            ("place_label", lambda: sa.String(256)), ("place_accuracy_m", sa.Float))


def upgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trip_expenses")}
    for name, typ in _COLUMNS:
        if name not in have:
            op.add_column("trip_expenses", sa.Column(name, typ()))


def downgrade() -> None:
    with op.batch_alter_table("trip_expenses") as batch:
        for name, _ in reversed(_COLUMNS):
            batch.drop_column(name)
