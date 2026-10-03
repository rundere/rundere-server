"""Run·dere 4.7 — saved places (YONDER-200, D-166).

A place saved from a Maps link before it belongs to any trip. Owner-scoped,
no foreign keys (house convention); filing it to a trip consumes the row.

Revision ID: 0032
Revises: 0031
"""
import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "saved_places" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "saved_places",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36)),
        sa.Column("place_key", sa.String(200), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("lat", sa.Float),
        sa.Column("lon", sa.Float),
        sa.Column("address", sa.String(500)),
        sa.Column("category", sa.String(80)),
        sa.Column("activity_type", sa.String(24), nullable=False),
        sa.Column("provider", sa.String(12), nullable=False),
        sa.Column("source_url", sa.String(2000)),
        sa.Column("notes", sa.Text),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_saved_places_owner_id", "saved_places", ["owner_id"])
    op.create_index("ix_saved_places_place_key", "saved_places", ["place_key"])


def downgrade() -> None:
    op.drop_table("saved_places")
