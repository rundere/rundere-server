"""Yonder 3.1 — the editable packing item catalog (D-84).

`packing_catalog_items` holds the item catalog as data the owner edits via the
admin UI, seeded from planning/data/items.json at startup. Separate migration
from the per-trip planning tables (0012) since this is reference/seed data with a
different lifecycle.

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("packing_catalog_items"):
        op.create_table(
            "packing_catalog_items",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("label", sa.String(128), nullable=False),
            sa.Column("category", sa.String(16), nullable=False),
            sa.Column("bag", sa.String(12), nullable=False, server_default="checked"),
            sa.Column("weight_g", sa.Integer, nullable=False, server_default="0"),
            sa.Column("tags", sa.JSON),
            sa.Column("variants", sa.JSON),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_packing_catalog_items_category",
                        "packing_catalog_items", ["category"])


def downgrade() -> None:
    if _has_table("packing_catalog_items"):
        op.drop_table("packing_catalog_items")
