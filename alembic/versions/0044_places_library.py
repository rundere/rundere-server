"""Run·dere — the Places Library (SPEC-11, YONDER-314, D-212 to D-216).

Saved places become a library. The migration:
- adds what makes a row a library entry: to-do, priority, visited, locality
  for the Atlas, a geohash, and search text;
- adds lists, list membership, and one row per trip a place was filed onto;
- turns every `filed` place back into `saved` (D-212: filing no longer consumes
  a place).

Where the activity a filed place became can still be found (the same owner's
trip, the same Maps link and name), the filing is recorded. Otherwise the place
simply returns to the library.

Locality, geohash and search text are filled by the app (`app/places.py`
backfill and the lazy refresh), not here, so the migration needs no network.

All links are soft, with no foreign keys (house rule 3).

Revision ID: 0044
Revises: 0043
"""
import uuid
from datetime import datetime, timezone
from urllib.parse import quote_plus

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("todo", sa.Text, {}),
    ("priority", sa.String(8), {"nullable": False, "server_default": "someday"}),
    ("visited_on", sa.String(10), {}),
    ("locality", sa.String(120), {}),
    ("region", sa.String(120), {}),
    ("country_code", sa.String(2), {}),
    ("locality_source", sa.String(8), {}),
    ("geohash", sa.String(9), {}),
    ("search_text", sa.Text, {"nullable": False, "server_default": ""}),
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    have = set(insp.get_table_names())
    cols = {c["name"] for c in insp.get_columns("saved_places")}
    with op.batch_alter_table("saved_places") as batch:
        for name, type_, kw in _COLUMNS:
            if name not in cols:
                batch.add_column(sa.Column(name, type_, **kw))
    idx = {i["name"] for i in insp.get_indexes("saved_places")}
    if "ix_saved_places_country_code" not in idx:
        op.create_index("ix_saved_places_country_code", "saved_places", ["country_code"])
    if "ix_saved_places_geohash" not in idx:
        op.create_index("ix_saved_places_geohash", "saved_places", ["geohash"])

    if "saved_place_filings" not in have:
        op.create_table(
            "saved_place_filings",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("owner_id", sa.String(36)),
            sa.Column("place_id", sa.String(36), nullable=False),
            sa.Column("trip_id", sa.String(36), nullable=False),
            sa.Column("activity_id", sa.String(36)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        for col in ("owner_id", "place_id", "trip_id", "activity_id"):
            op.create_index(f"ix_saved_place_filings_{col}", "saved_place_filings", [col])
        op.create_index("uq_saved_place_filings_place_trip", "saved_place_filings",
                        ["place_id", "trip_id"], unique=True)
    if "place_lists" not in have:
        op.create_table(
            "place_lists",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("owner_id", sa.String(36)),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("emoji", sa.String(16)),
            sa.Column("description", sa.Text),
            sa.Column("position", sa.Integer, nullable=False, server_default="0"),
            sa.Column("source", sa.String(160)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_place_lists_owner_id", "place_lists", ["owner_id"])
    if "place_list_members" not in have:
        op.create_table(
            "place_list_members",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("owner_id", sa.String(36)),
            sa.Column("list_id", sa.String(36), nullable=False),
            sa.Column("place_id", sa.String(36), nullable=False),
            sa.Column("position", sa.Integer, nullable=False, server_default="0"),
            sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        )
        for col in ("owner_id", "list_id", "place_id"):
            op.create_index(f"ix_place_list_members_{col}", "place_list_members", [col])
        op.create_index("uq_place_list_members_list_place", "place_list_members",
                        ["list_id", "place_id"], unique=True)

    _unconsume_filed(bind)


def _map_link(source_url, lat, lon, name) -> str:
    """What app/places.map_link gave the activity when the place was filed."""
    if source_url:
        return source_url
    q = f"{lat},{lon}" if lat is not None else name
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(q)}"


def _unconsume_filed(bind) -> None:
    """D-212: a filed place goes back to the library, with its filing recorded
    when the activity it became can still be recognised."""
    places = sa.table("saved_places", sa.column("id"), sa.column("owner_id"),
                      sa.column("status"), sa.column("name"), sa.column("lat"),
                      sa.column("lon"), sa.column("source_url"))
    trips = sa.table("trips", sa.column("id"), sa.column("owner_id"))
    acts = sa.table("trip_activities", sa.column("id"), sa.column("trip_id"),
                    sa.column("values", sa.JSON))
    filings = sa.table("saved_place_filings", sa.column("id"), sa.column("owner_id"),
                       sa.column("place_id"), sa.column("trip_id"),
                       sa.column("activity_id"), sa.column("created_at"))
    filed = bind.execute(sa.select(places).where(places.c.status == "filed")).fetchall()
    taken: set[str] = set()
    now = datetime.now(timezone.utc)
    for p in filed:
        link = _map_link(p.source_url, p.lat, p.lon, p.name)
        owner_trips = [t.id for t in bind.execute(
            sa.select(trips.c.id).where(trips.c.owner_id == p.owner_id))]
        match = None
        if owner_trips:
            for a in bind.execute(sa.select(acts).where(acts.c.trip_id.in_(owner_trips))):
                v = a.values or {}
                if (a.id not in taken and v.get("name") == p.name
                        and link in (v.get("mapLink"), v.get("sourceLink"))):
                    match = a
                    break
        if match is not None:
            taken.add(match.id)
            bind.execute(sa.insert(filings).values(
                id=str(uuid.uuid4()), owner_id=p.owner_id, place_id=p.id,
                trip_id=match.trip_id, activity_id=match.id, created_at=now))
    bind.execute(sa.update(places).where(places.c.status == "filed").values(status="saved"))


def downgrade() -> None:
    # Filed places come back as `filed` so the old code keeps treating them as handled.
    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE saved_places SET status = 'filed' WHERE status = 'saved' AND id IN "
        "(SELECT place_id FROM saved_place_filings)"))
    op.drop_table("place_list_members")
    op.drop_table("place_lists")
    op.drop_table("saved_place_filings")
    op.drop_index("ix_saved_places_geohash", table_name="saved_places")
    op.drop_index("ix_saved_places_country_code", table_name="saved_places")
    with op.batch_alter_table("saved_places") as batch:
        for name, _, _ in reversed(_COLUMNS):
            batch.drop_column(name)
