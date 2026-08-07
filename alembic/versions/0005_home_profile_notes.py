"""Add home_profiles.notes (used by the purpose-built home editor, D-39).

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if _has_column("home_profiles", "notes"):
        return
    op.add_column("home_profiles", sa.Column("notes", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("home_profiles", "notes")
