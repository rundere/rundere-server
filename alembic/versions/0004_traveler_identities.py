"""traveler_identities — who is 'self' vs a guest (D-22/RULE-10).

Until now `self_on_booking` was hardcoded True in the resolver, so RULE-10
(guest-only flights are never written to AirTrail) could not fire at all.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")




def _has_column(table: str, column: str) -> bool:
    """The 0001 baseline builds tables from the CURRENT models via create_all,
    so on a FRESH database it already contains everything this migration would
    add, and the ALTER would fail. On an existing database it does not. Check
    rather than assume, so both paths reach head."""
    import sqlalchemy as sa
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def _has_table(table: str) -> bool:
    import sqlalchemy as sa
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("traveler_identities"):
        return
    op.create_table(
        "traveler_identities",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("display_name", sa.String(128), nullable=False, unique=True),
        sa.Column("kind", sa.String(8), nullable=False, server_default="guest"),
        sa.Column("aliases", _JSON),
        sa.Column("airtrail_user_id", sa.String(64)),
        sa.Column("notes", sa.String(512)),
    )


def downgrade() -> None:
    op.drop_table("traveler_identities")
