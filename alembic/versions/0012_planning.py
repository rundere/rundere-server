"""Yonder 3.1 — Planning: packing profiles, plans, items.

Three tables backing the Packing Plan (SPEC-3.1-planning.md §3):

  packing_profiles  the "ask once" preferences, per-traveller (single-user for now)
  packing_plans     one generated plan per trip (peer of discovery_runs)
  packing_items     the checklist lines (peer of discovery_findings), idempotent
                    on (trip_id, item_key), enforced in app code — same pattern
                    as the discovery tables, identical on SQLite and Postgres.

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("packing_profiles"):
        op.create_table(
            "packing_profiles",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("traveler_id", sa.String(36)),
            sa.Column("values", sa.JSON),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_packing_profiles_traveler_id", "packing_profiles",
                        ["traveler_id"])

    if not _has_table("packing_plans"):
        op.create_table(
            "packing_plans",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("trip_id", sa.String(36), nullable=False),
            sa.Column("status", sa.String(12), nullable=False, server_default="running"),
            sa.Column("archetypes", sa.JSON),
            sa.Column("context", sa.JSON),
            sa.Column("warnings", sa.JSON),
            sa.Column("tasks", sa.JSON),
            sa.Column("error", sa.Text),
            sa.Column("generated_at", sa.DateTime(timezone=True)),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_packing_plans_trip_id", "packing_plans", ["trip_id"])
        op.create_index("ix_packing_plans_status", "packing_plans", ["status"])

    if not _has_table("packing_items"):
        op.create_table(
            "packing_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("plan_id", sa.String(36)),
            sa.Column("trip_id", sa.String(36), nullable=False),
            sa.Column("item_key", sa.String(64), nullable=False),
            sa.Column("label", sa.String(128), nullable=False),
            sa.Column("category", sa.String(16), nullable=False),
            sa.Column("quantity", sa.Integer, nullable=False, server_default="1"),
            sa.Column("bag", sa.String(12)),
            sa.Column("reason", sa.String(256)),
            sa.Column("source", sa.String(16)),
            sa.Column("swap_for", sa.String(128)),
            sa.Column("status", sa.String(12), nullable=False, server_default="draft"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_packing_items_plan_id", "packing_items", ["plan_id"])
        op.create_index("ix_packing_items_trip_id", "packing_items", ["trip_id"])
        op.create_index("ix_packing_items_category", "packing_items", ["category"])
        op.create_index("ix_packing_items_idem", "packing_items",
                        ["trip_id", "item_key"])


def downgrade() -> None:
    for t in ("packing_items", "packing_plans", "packing_profiles"):
        if _has_table(t):
            op.drop_table(t)
