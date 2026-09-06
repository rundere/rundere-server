"""Add traveler_identities.base_currency — the traveller's explicit base
currency for discovery currency conversion (D-65), independent of nationality
and home profile.

Revision ID: 0023
Revises: 0022
"""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if _has_column("traveler_identities", "base_currency"):
        return
    op.add_column("traveler_identities",
                  sa.Column("base_currency", sa.String(3), nullable=True))


def downgrade() -> None:
    op.drop_column("traveler_identities", "base_currency")
