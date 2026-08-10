"""Yonder 2.1 — link a Trip to an AdventureLog collection.

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if not _has_column("trips", "adventurelog_collection_id"):
        op.add_column("trips", sa.Column("adventurelog_collection_id",
                                         sa.String(64), nullable=True))


def downgrade() -> None:
    if _has_column("trips", "adventurelog_collection_id"):
        op.drop_column("trips", "adventurelog_collection_id")
