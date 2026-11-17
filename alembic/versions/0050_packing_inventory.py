"""Run·dere — packing inventory and templates (SPEC-5 §6.6, D-127, D-229–D-231).

New: `inventory_items` (what a person owns), `packing_templates` and
`packing_template_items` (their own kits). Packing lines gain
`inventory_item_id`, bags gain `user_id`, and the catalogue gains the
questionnaire's seed columns. Every link is a plain indexed column, no foreign
keys (house rule 3). Existing bags stay ownerless, so only admins see them
until their owner takes the questionnaire; catalogue seed columns are filled
from items.json at startup (planning.catalog.seed_catalog), not here, so owner
edits made since are never touched.

Revision ID: 0050
Revises: 0049
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    have = set(sa.inspect(bind).get_table_names())

    if "inventory_items" not in have:
        op.create_table(
            "inventory_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36)),
            sa.Column("type_key", sa.String(64)),
            sa.Column("label", sa.String(128), nullable=False),
            sa.Column("category", sa.String(16), nullable=False),
            sa.Column("tags", _JSON),
            sa.Column("quantity", sa.Integer, nullable=False, server_default="1"),
            sa.Column("must_have", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("brand", sa.String(64)),
            sa.Column("notes", sa.Text),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
            # Unused until 5.x (P14, D-128/D-129).
            sa.Column("kind", sa.String(12)),
            sa.Column("usage_rate", _JSON),
            sa.Column("reorder_threshold", sa.Integer),
            sa.Column("reorder_url", sa.String(512)),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_inventory_items_user_id", "inventory_items", ["user_id"])
        op.create_index("ix_inventory_items_type_key", "inventory_items", ["type_key"])
        op.create_index("ix_inventory_items_category", "inventory_items", ["category"])

    if "packing_templates" not in have:
        op.create_table(
            "packing_templates",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36)),
            sa.Column("type_key", sa.String(32), nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("keyword", sa.String(64)),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_packing_templates_user_id", "packing_templates", ["user_id"])
        op.create_index("ix_packing_templates_type_key", "packing_templates", ["type_key"])

    if "packing_template_items" not in have:
        op.create_table(
            "packing_template_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("template_id", sa.String(36), nullable=False),
            sa.Column("inventory_item_id", sa.String(36), nullable=False),
            sa.Column("quantity", sa.Integer),
        )
        op.create_index("ix_packing_template_items_template_id",
                        "packing_template_items", ["template_id"])
        op.create_index("ix_packing_template_items_inventory_item_id",
                        "packing_template_items", ["inventory_item_id"])

    if "packing_items" in have and "inventory_item_id" not in _columns(bind, "packing_items"):
        with op.batch_alter_table("packing_items") as b:
            b.add_column(sa.Column("inventory_item_id", sa.String(36)))
        op.create_index("ix_packing_items_inventory_item_id", "packing_items",
                        ["inventory_item_id"])

    if "bags" in have and "user_id" not in _columns(bind, "bags"):
        with op.batch_alter_table("bags") as b:
            b.add_column(sa.Column("user_id", sa.String(36)))
        op.create_index("ix_bags_user_id", "bags", ["user_id"])

    if "packing_catalog_items" in have:
        cols = _columns(bind, "packing_catalog_items")
        with op.batch_alter_table("packing_catalog_items") as b:
            if "seed" not in cols:
                b.add_column(sa.Column("seed", _JSON))
            if "styles" not in cols:
                b.add_column(sa.Column("styles", _JSON))
            if "seed_qty" not in cols:
                b.add_column(sa.Column("seed_qty", sa.Integer))
            if "minimal" not in cols:
                b.add_column(sa.Column("minimal", sa.Boolean, nullable=False,
                                       server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("packing_catalog_items") as b:
        for c in ("minimal", "seed_qty", "styles", "seed"):
            b.drop_column(c)
    op.drop_index("ix_bags_user_id", "bags")
    with op.batch_alter_table("bags") as b:
        b.drop_column("user_id")
    op.drop_index("ix_packing_items_inventory_item_id", "packing_items")
    with op.batch_alter_table("packing_items") as b:
        b.drop_column("inventory_item_id")
    op.drop_table("packing_template_items")
    op.drop_table("packing_templates")
    op.drop_table("inventory_items")
