"""Yonder 3.9 — per-trip packing line edits (quantity/bag/label) + custom lines.

`packing_items.edited` marks a row a human hand-edited (quantity, bag, label,
or a fully custom line added on the trip) — orthogonal to `status`. A
re-generate already never touches `packed`/`removed` rows; this extends that
same protection to an `edited` row that's still nominally "draft", so a
quantity tweak or bag reassignment survives the next Packing Plan regenerate
instead of being silently overwritten by the deterministic engine.

Revision ID: 0017
Revises: 0016
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if not _has_column("packing_items", "edited"):
        op.add_column("packing_items",
                      sa.Column("edited", sa.Boolean(), nullable=False,
                                server_default=sa.false()))


def downgrade() -> None:
    if _has_column("packing_items", "edited"):
        op.drop_column("packing_items", "edited")
