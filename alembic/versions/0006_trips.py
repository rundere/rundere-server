"""Yonder 2.0 — in-house trips: trips + trip_activities.

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# JSONB on Postgres, plain JSON elsewhere — same variant the models use.
JsonType = sa.JSON().with_variant(JSONB(), "postgresql")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _tables()
    if "trips" not in existing:
        op.create_table(
            "trips",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("start_date", sa.String(10), nullable=True),
            sa.Column("end_date", sa.String(10), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("source", sa.String(16), nullable=False,
                      server_default="manual"),
            sa.Column("external_ref", sa.String(128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_trips_external_ref", "trips", ["external_ref"])
    if "trip_activities" not in existing:
        op.create_table(
            "trip_activities",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("trip_id", sa.String(36),
                      sa.ForeignKey("trips.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("type", sa.String(24), nullable=False),
            sa.Column("starts_at", sa.String(32), nullable=True),
            sa.Column("values", JsonType, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_trip_activities_trip_id", "trip_activities",
                        ["trip_id"])
        op.create_index("ix_trip_activities_starts_at", "trip_activities",
                        ["starts_at"])


def downgrade() -> None:
    for tbl in ("trip_activities", "trips"):
        if tbl in _tables():
            op.drop_table(tbl)
