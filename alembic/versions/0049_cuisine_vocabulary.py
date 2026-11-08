"""Run·dere — Discovery profiles' cuisines become a vocabulary (YONDER-328).

"Cuisines to avoid" was a comma-separated text box. 5.0 replaces it with the
OpenStreetMap cuisine list (discover/data/cuisines.json), each marked Like,
Neutral or Don't like. This converts what people typed: a value that names a
cuisine ("Sushi", "fast food", "BBQ") becomes its key, and anything else moves
to the profile's notes for agents, so nothing anyone wrote is lost. Safe to
re-run: a profile whose dislikes are all keys is left alone.

Revision ID: 0049
Revises: 0048
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")
_PROFILES = sa.table("discovery_profiles", sa.column("id", sa.String),
                     sa.column("values", _JSON))
NOTES_MAX = 2000                     # app.trips.routes.DISCOVERY_NOTES_MAX


def convert(values: dict) -> dict | None:
    """The converted profile values, or None when there's nothing to change."""
    from discover import cuisines
    food = dict((values or {}).get("food") or {})
    old = [str(x) for x in food.get("cuisine_dislikes") or []]
    known = set(cuisines.labels())
    if all(x in known for x in old):
        return None
    keys, unmatched = [], []
    for text in old:
        key = text if text in known else cuisines.match(text)
        if key:
            keys.append(key)
        elif text.strip():
            unmatched.append(text.strip())
    food["cuisine_dislikes"] = cuisines.keys_only(keys)
    out = {**values, "food": food}
    if unmatched:
        note = "Cuisines to avoid (from before 5.0): " + ", ".join(unmatched)
        notes = (values.get("notes") or "").strip()
        out["notes"] = ((notes + "\n" if notes else "") + note)[:NOTES_MAX]
    return out


def upgrade() -> None:
    bind = op.get_bind()
    if "discovery_profiles" not in sa.inspect(bind).get_table_names():
        return
    for row in bind.execute(sa.select(_PROFILES.c.id, _PROFILES.c["values"])).all():
        new = convert(row[1] or {})
        if new is not None:
            bind.execute(_PROFILES.update().where(_PROFILES.c.id == row[0])
                         .values({"values": new}))


def downgrade() -> None:
    # Keys still read as text ("sushi"), and moved notes stay where people can
    # see them, so there is nothing to undo.
    pass
