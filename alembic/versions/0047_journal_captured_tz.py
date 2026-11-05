"""Run·dere — a journal entry keeps the zone it was written in (D-224, YONDER-355).

`captured_tz` is the IANA zone, so the web and the apps show the note's time as
the writer's clock read it. Existing rows stay NULL: their zone is worked out
from place_lat/lon when they're shown.

Revision ID: 0047
Revises: 0046
"""
import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("journal_entries")}
    if "captured_tz" not in cols:
        with op.batch_alter_table("journal_entries") as batch:
            batch.add_column(sa.Column("captured_tz", sa.String(64)))


def downgrade() -> None:
    with op.batch_alter_table("journal_entries") as batch:
        batch.drop_column("captured_tz")
