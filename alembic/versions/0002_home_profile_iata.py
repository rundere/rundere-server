"""Add home_profiles.iata for airport-based home matching (D-07).

Nullable, so existing rows are unaffected; only kind=home_airport profiles
populate it. resolve/home.py matches exact codes and IATA metro codes
(YTO -> YYZ/YTZ, WAS -> DCA/IAD/BWI).

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None




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
    if _has_column("home_profiles", "iata"):
        return
    op.add_column("home_profiles", sa.Column("iata", sa.String(3), nullable=True))


def downgrade() -> None:
    op.drop_column("home_profiles", "iata")
