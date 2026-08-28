"""Yonder 4.0 Phase 2 — per-user packing (SPEC-4 D-123).

Packing plans/items are generated for and tied to the viewing user (keyed by
trip_id + user_id) — no cross-visibility between travellers. The PackingProfile
(preferences) is already traveler_id-scoped; generation now uses the viewing
user's own profile via their traveller. Discovery stays trip-wide/joint.

Both columns nullable; NULL = legacy/auth-off shared plan. No backfill.

Revision ID: 0022
Revises: 0021
"""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def _cols(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "user_id" not in _cols("packing_plans"):
        op.add_column("packing_plans", sa.Column("user_id", sa.String(36), nullable=True))
        op.create_index("ix_packing_plans_user_id", "packing_plans", ["user_id"])
    if "user_id" not in _cols("packing_items"):
        op.add_column("packing_items", sa.Column("user_id", sa.String(36), nullable=True))
        op.create_index("ix_packing_items_user_id", "packing_items", ["user_id"])


def downgrade() -> None:
    for table, idx in (("packing_items", "ix_packing_items_user_id"),
                       ("packing_plans", "ix_packing_plans_user_id")):
        if "user_id" in _cols(table):
            op.drop_index(idx, table_name=table)
            op.drop_column(table, "user_id")
