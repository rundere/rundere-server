"""Yonder 2.1.2 — link a captured TravelItem to an in-house Yonder Trip.

A soft, independent link: Yonder trips are a first-class trip system in their own
right (not a mirror of AdventureLog), so a review item can be filed into a Yonder
trip regardless of whether it also resolves to an AdventureLog collection. Plain
nullable indexed column, no DB-level FK — matches how the AdventureLog link was
added (0007) and keeps the migration identical on SQLite (dev) and Postgres
(prod); a deleted trip has its links cleared in app code (trips delete route).

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if not _has_column("travel_items", "trip_id"):
        op.add_column("travel_items",
                      sa.Column("trip_id", sa.String(36), nullable=True))
        op.create_index("ix_travel_items_trip_id", "travel_items", ["trip_id"])


def downgrade() -> None:
    if _has_column("travel_items", "trip_id"):
        op.drop_index("ix_travel_items_trip_id", table_name="travel_items")
        op.drop_column("travel_items", "trip_id")
