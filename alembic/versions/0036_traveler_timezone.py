"""Run·dere 4.8 — a person's own time zone on their travel profile
(YONDER-219, D-187).

Revision ID: 0036
Revises: 0035
"""
import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("traveler_identities")}
    if "timezone" not in have:
        op.add_column("traveler_identities", sa.Column("timezone", sa.String(64)))


def downgrade() -> None:
    with op.batch_alter_table("traveler_identities") as batch:
        batch.drop_column("timezone")
