"""Run·dere — one dated exchange-rate table per trip (YONDER-237, SPEC-10 D-195):
`trip_rates`, and the date + row each expense and planned item was converted
with. Stored rates are kept exactly as they are; rows are filled on demand.

Revision ID: 0040
Revises: 0039
"""
import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

_COSTS = ("trip_expenses", "trip_budget_items")


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "trip_rates" not in set(insp.get_table_names()):
        op.create_table(
            "trip_rates",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("trip_id", sa.String(36), nullable=False),
            sa.Column("currency", sa.String(3), nullable=False),
            sa.Column("quote", sa.String(3), nullable=False),
            sa.Column("rate_date", sa.String(10), nullable=False),
            sa.Column("rate", sa.String(32), nullable=False),
            sa.Column("as_of", sa.String(10)),
            sa.Column("source", sa.String(12), nullable=False),
            sa.Column("fetched_by", sa.String(36)),
            sa.Column("fetched_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("trip_id", "currency", "quote", "rate_date",
                                name="uq_trip_rate_day"),
        )
        op.create_index("ix_trip_rates_trip_id", "trip_rates", ["trip_id"])
    for table in _COSTS:
        cols = {c["name"] for c in insp.get_columns(table)}
        with op.batch_alter_table(table) as b:
            if "rate_date" not in cols:
                b.add_column(sa.Column("rate_date", sa.String(10)))
            if "rate_id" not in cols:
                b.add_column(sa.Column("rate_id", sa.String(36)))


def downgrade() -> None:
    for table in _COSTS:
        with op.batch_alter_table(table) as b:
            b.drop_column("rate_id")
            b.drop_column("rate_date")
    op.drop_table("trip_rates")
