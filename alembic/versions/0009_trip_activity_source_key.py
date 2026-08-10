"""Yonder 2.2.0 — dedup key so a captured item can be logged to a Yonder trip
idempotently as a TripActivity.

`source_item_key` records the originating capture's canonical_key. The commit
stage upserts on (trip_id, source_item_key): re-committing or re-capturing the
same booking updates that one activity instead of duplicating it, and a
hand-made activity (source_item_key NULL) never collides with a captured one.
Plain nullable indexed column, uniqueness enforced in app code — same pattern as
0007/0008, identical on SQLite (dev) and Postgres (prod).

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if not _has_column("trip_activities", "source_item_key"):
        op.add_column("trip_activities",
                      sa.Column("source_item_key", sa.String(512), nullable=True))
        op.create_index("ix_trip_activities_source_item_key",
                        "trip_activities", ["source_item_key"])


def downgrade() -> None:
    if _has_column("trip_activities", "source_item_key"):
        op.drop_index("ix_trip_activities_source_item_key",
                      table_name="trip_activities")
        op.drop_column("trip_activities", "source_item_key")
