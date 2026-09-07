"""Yonder 4.x — Travel Journal (SPEC-6).

A new per-user `journal_entries` table: quick, first-person, dated notes the
traveller authors. Additive and orthogonal — it touches no existing table and
never joins the capture pipeline (D-131). `owner_id` and `trip_id` are nullable
soft links (no DB FK — house convention). Personal scope lives in app/scope.py.

Revision ID: 0024
Revises: 0023
"""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("journal_entries"):
        return
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), nullable=True),
        sa.Column("entry_date", sa.String(10), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trip_id", sa.String(36), nullable=True),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("values", sa.JSON(), nullable=True),
        sa.Column("place_lat", sa.Float(), nullable=True),
        sa.Column("place_lon", sa.Float(), nullable=True),
        sa.Column("place_label", sa.String(256), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="api"),
        sa.Column("duplicated_from", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_journal_entries_owner_id", "journal_entries", ["owner_id"])
    op.create_index("ix_journal_entries_entry_date", "journal_entries", ["entry_date"])
    op.create_index("ix_journal_entries_trip_id", "journal_entries", ["trip_id"])
    op.create_index("ix_journal_entries_duplicated_from", "journal_entries",
                    ["duplicated_from"])


def downgrade() -> None:
    if _has_table("journal_entries"):
        op.drop_table("journal_entries")
