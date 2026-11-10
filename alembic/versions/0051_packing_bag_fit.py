"""Run·dere — bags for a trip, and what fits (SPEC-5 §6.10, D-232).

`packing_plans.bag_ids` holds the bags a person picked for the trip, carried
across re-generates like the template pick. `volume_l` on the catalogue and on
inventory items is a rough packed size in litres; NULL means the shipped
estimate (items.json) or the category default, so nothing is filled here.

Revision ID: 0051
Revises: 0050
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    have = set(sa.inspect(bind).get_table_names())
    for table, col in (("packing_plans", sa.Column("bag_ids", _JSON)),
                       ("packing_catalog_items", sa.Column("volume_l", sa.Float)),
                       ("inventory_items", sa.Column("volume_l", sa.Float))):
        if table in have and col.name not in _columns(bind, table):
            with op.batch_alter_table(table) as b:
                b.add_column(col)


def downgrade() -> None:
    for table, col in (("inventory_items", "volume_l"),
                       ("packing_catalog_items", "volume_l"),
                       ("packing_plans", "bag_ids")):
        with op.batch_alter_table(table) as b:
            b.drop_column(col)
