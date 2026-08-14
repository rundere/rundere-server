"""Yonder 3.0.1 — Discovery place exclusions.

A per-trip list of derived places NOT to research (layover cities, mis-geocoded
stops). Plain soft link on trip_id; uniqueness of (trip_id, place_key) is enforced
in app code — same pattern as the other discovery tables, identical on SQLite
(dev) and Postgres (prod).

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("discovery_exclusions"):
        op.create_table(
            "discovery_exclusions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("trip_id", sa.String(36), nullable=False),
            sa.Column("place_key", sa.String(128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_discovery_exclusions_trip_id",
                        "discovery_exclusions", ["trip_id"])
        op.create_index("ix_discovery_exclusions_place_key",
                        "discovery_exclusions", ["place_key"])


def downgrade() -> None:
    if _has_table("discovery_exclusions"):
        op.drop_table("discovery_exclusions")
