"""Yonder 3.2.1 — per-trip archetype override on the packing plan.

`packing_plans.manual_archetypes` holds the operator's explicit archetype choice
(a JSON list of ids) so auto-detection can be confirmed/adjusted — the same place
in different seasons is a different trip (Banff ski vs city). NULL = auto-detect.

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if not _has_column("packing_plans", "manual_archetypes"):
        op.add_column("packing_plans",
                      sa.Column("manual_archetypes", _JSON, nullable=True))


def downgrade() -> None:
    if _has_column("packing_plans", "manual_archetypes"):
        op.drop_column("packing_plans", "manual_archetypes")
