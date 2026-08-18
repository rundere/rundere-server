"""Yonder 3.2 — TravelerIdentity as the person spine (D-94, SPEC-3.2 §1/§9).

Promotes TravelerIdentity to the central person record every profile hangs off:
  - traveler_identities.nationality (D-91) — ISO-2, used by the visa resolver.
  - home_profiles.traveler_id (D-94) — soft link, NULL = deployment-wide default.
  - packing_profiles.traveler_id — legacy String(36) converted to Integer so the
    soft link is type-coherent with traveler_identities.id (always NULL to date).
  - discovery_profiles (new, D-89) — the "ask once" discovery preferences.
  - trip_travelers (new, D-94) — structured who-is-on-this-trip roster, replacing
    the free-text `traveling_with` hint.

All additions are nullable with no default — zero disruption to existing data.
Every step is guarded so both the fresh-DB path (0001 create_all already builds
the current models) and an existing database reach head.

Revision ID: 0014
Revises: 0013
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def _column_type(table: str, column: str) -> str:
    """Uppercased type name of a column, e.g. 'VARCHAR', 'INTEGER', ''."""
    insp = sa.inspect(op.get_bind())
    for c in insp.get_columns(table):
        if c["name"] == column:
            return str(c["type"]).upper()
    return ""


def upgrade() -> None:
    # 1. traveler_identities.nationality (ISO-2) + generic-guest flag
    if _has_table("traveler_identities") and not _has_column(
            "traveler_identities", "nationality"):
        op.add_column("traveler_identities",
                      sa.Column("nationality", sa.String(2), nullable=True))
    if _has_table("traveler_identities") and not _has_column(
            "traveler_identities", "generic"):
        op.add_column("traveler_identities",
                      sa.Column("generic", sa.Boolean, nullable=False,
                                server_default="0"))

    # 2. home_profiles.traveler_id (soft link)
    if _has_table("home_profiles") and not _has_column(
            "home_profiles", "traveler_id"):
        op.add_column("home_profiles",
                      sa.Column("traveler_id", sa.Integer, nullable=True))
        op.create_index("ix_home_profiles_traveler_id",
                        "home_profiles", ["traveler_id"])

    # 3. packing_profiles.traveler_id String(36) -> Integer (always NULL to date).
    #    On a fresh DB it is already Integer (create_all of current models); only
    #    convert when the legacy character type is still present.
    if _has_table("packing_profiles") and "CHAR" in _column_type(
            "packing_profiles", "traveler_id"):
        bind = op.get_bind()
        if bind.dialect.name == "postgresql":
            op.alter_column(
                "packing_profiles", "traveler_id",
                type_=sa.Integer,
                postgresql_using="traveler_id::integer",
                existing_nullable=True,
            )
        else:
            # SQLite cannot ALTER a column type in place; batch mode rebuilds it.
            with op.batch_alter_table("packing_profiles") as batch:
                batch.alter_column(
                    "traveler_id",
                    type_=sa.Integer,
                    existing_type=sa.String(36),
                    existing_nullable=True,
                )

    # 4. discovery_profiles (new)
    if not _has_table("discovery_profiles"):
        op.create_table(
            "discovery_profiles",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("traveler_id", sa.Integer, nullable=True),
            sa.Column("values", _JSON),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_discovery_profiles_traveler_id",
                        "discovery_profiles", ["traveler_id"])

    # 5. trip_travelers (new)
    if not _has_table("trip_travelers"):
        op.create_table(
            "trip_travelers",
            sa.Column("trip_id", sa.String(36),
                      sa.ForeignKey("trips.id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("traveler_id", sa.Integer, primary_key=True),
            sa.Column("role", sa.String(16), nullable=False,
                      server_default="companion"),
        )


def downgrade() -> None:
    if _has_table("trip_travelers"):
        op.drop_table("trip_travelers")
    if _has_table("discovery_profiles"):
        op.drop_table("discovery_profiles")
    if _has_column("packing_profiles", "traveler_id") and "INT" in _column_type(
            "packing_profiles", "traveler_id"):
        bind = op.get_bind()
        if bind.dialect.name == "postgresql":
            op.alter_column(
                "packing_profiles", "traveler_id",
                type_=sa.String(36),
                postgresql_using="traveler_id::varchar",
                existing_nullable=True,
            )
        else:
            with op.batch_alter_table("packing_profiles") as batch:
                batch.alter_column(
                    "traveler_id",
                    type_=sa.String(36),
                    existing_type=sa.Integer,
                    existing_nullable=True,
                )
    if _has_column("home_profiles", "traveler_id"):
        op.drop_index("ix_home_profiles_traveler_id", table_name="home_profiles")
        op.drop_column("home_profiles", "traveler_id")
    if _has_column("traveler_identities", "generic"):
        op.drop_column("traveler_identities", "generic")
    if _has_column("traveler_identities", "nationality"):
        op.drop_column("traveler_identities", "nationality")
