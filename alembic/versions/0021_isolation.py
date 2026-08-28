"""Yonder 4.0 Phase 2 — per-user data isolation & trip sharing (SPEC-4 D-119–121).

Two ownership roots: Trip (owner + trip_members sharing) and TravelItem (capture,
owned via the ingesting key/proxy). All columns nullable; NULL = open. No
backfill — existing trips and captures stay open until claimed/assigned.

Revision ID: 0021
Revises: 0020
"""
import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def _cols(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if "owner_id" not in _cols("trips"):
        op.add_column("trips", sa.Column("owner_id", sa.String(36), nullable=True))
        op.create_index("ix_trips_owner_id", "trips", ["owner_id"])
    if "owner_id" not in _cols("travel_items"):
        op.add_column("travel_items", sa.Column("owner_id", sa.String(36), nullable=True))
        op.create_index("ix_travel_items_owner_id", "travel_items", ["owner_id"])
    if "user_id" not in _cols("ingest_batches"):
        op.add_column("ingest_batches", sa.Column("user_id", sa.String(36), nullable=True))
        op.create_index("ix_ingest_batches_user_id", "ingest_batches", ["user_id"])
    if "user_id" not in _cols("api_keys"):
        op.add_column("api_keys", sa.Column("user_id", sa.String(36), nullable=True))
        op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    if not _has_table("trip_members"):
        op.create_table(
            "trip_members",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("trip_id", sa.String(36), nullable=False),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("role", sa.String(16), nullable=False, server_default="member"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("trip_id", "user_id", name="uq_trip_member"),
        )
        op.create_index("ix_trip_members_trip_id", "trip_members", ["trip_id"])
        op.create_index("ix_trip_members_user_id", "trip_members", ["user_id"])


def downgrade() -> None:
    if _has_table("trip_members"):
        op.drop_table("trip_members")
    for table, col, idx in (("api_keys", "user_id", "ix_api_keys_user_id"),
                            ("ingest_batches", "user_id", "ix_ingest_batches_user_id"),
                            ("travel_items", "owner_id", "ix_travel_items_owner_id"),
                            ("trips", "owner_id", "ix_trips_owner_id")):
        if col in _cols(table):
            op.drop_index(idx, table_name=table)
            op.drop_column(table, col)
