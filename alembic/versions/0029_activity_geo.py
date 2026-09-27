"""Yonder 4.2.x — where each timeline activity is (owner, 2026-09-26).

A nullable `geo` JSON column on trip_activities: the activity's located points
(start/end or the single place, with lat/lon and where each came from) and, for
a road trip, the route line (encoded polyline) with its distance and time.
Computed from the activity's own text and map link when it is saved (Photon +
the router), or carried over from a capture's already-located payload at
commit. NULL for existing rows until `python -m app.cli locate-activities`
backfills them; everything that reads it treats NULL as "not located yet".

Revision ID: 0029
Revises: 0028
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    cols = sa.inspect(op.get_bind()).get_columns(table)
    return any(c["name"] == column for c in cols)


def upgrade() -> None:
    if _has_column("trip_activities", "geo"):
        return
    op.add_column("trip_activities", sa.Column(
        "geo", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True))


def downgrade() -> None:
    op.drop_column("trip_activities", "geo")
