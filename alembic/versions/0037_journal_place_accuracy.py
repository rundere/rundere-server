"""Run·dere 4.8.x — GPS accuracy on journal entries, as on expenses (parity
with trip_expenses.place_accuracy_m, YONDER-212 follow-up).

Revision ID: 0037
Revises: 0036
"""
import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("journal_entries")}
    if "place_accuracy_m" not in have:
        op.add_column("journal_entries", sa.Column("place_accuracy_m", sa.Float))


def downgrade() -> None:
    with op.batch_alter_table("journal_entries") as batch:
        batch.drop_column("place_accuracy_m")
