"""Yonder 4.1 — who each timeline activity is for.

A new `trip_activity_travelers(activity_id, traveler_id)` link table: on a trip
with several travellers, each activity can name the ones it belongs to (two
people on different flights, one hotel booked for part of the group). NO rows
for an activity means it's for everyone on the trip, so every existing activity
keeps its meaning without a backfill. Soft links, no DB FK (house convention);
app code deletes the rows alongside their activity or roster entry.

Revision ID: 0025
Revises: 0024
"""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("trip_activity_travelers"):
        return
    op.create_table(
        "trip_activity_travelers",
        sa.Column("activity_id", sa.String(36), primary_key=True),
        sa.Column("traveler_id", sa.Integer(), primary_key=True),
    )
    op.create_index("ix_trip_activity_travelers_traveler_id",
                    "trip_activity_travelers", ["traveler_id"])


def downgrade() -> None:
    op.drop_index("ix_trip_activity_travelers_traveler_id",
                  table_name="trip_activity_travelers")
    op.drop_table("trip_activity_travelers")
