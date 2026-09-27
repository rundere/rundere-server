"""Run·dere 4.5 — Discovery items (Discovery 2.0, YONDER-147).

Places, events and activities inside Food & Drink and Events & Activities
findings become their own rows, so each can be hidden, edited and added to the
timeline. Soft links only (no FKs — house convention). Existing food/events
findings are backfilled from what their resolvers already stored in `data`
(food `places`, events `impacts`/`notable`), using the same mapping every
later run uses (discover/items.rows_from_finding).

Revision ID: 0030
Revises: 0029
"""
import json
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("discovery_items"):
        op.create_table(
            "discovery_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("finding_id", sa.String(36), nullable=False),
            sa.Column("trip_id", sa.String(36), nullable=False),
            sa.Column("user_id", sa.String(36)),
            sa.Column("topic", sa.String(24), nullable=False),
            sa.Column("place_key", sa.String(128), nullable=False),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("name", sa.String(300), nullable=False),
            sa.Column("neighbourhood", sa.String(200)),
            sa.Column("address", sa.String(500)),
            sa.Column("lat", sa.Float()),
            sa.Column("lon", sa.Float()),
            sa.Column("website", sa.String(2000)),
            sa.Column("what_to_get", sa.Text()),
            sa.Column("price_tier", sa.Integer()),
            sa.Column("starts_on", sa.String(16)),
            sa.Column("ends_on", sa.String(16)),
            sa.Column("when_text", sa.String(200)),
            sa.Column("venue", sa.String(300)),
            sa.Column("details", sa.Text()),
            sa.Column("duration", sa.String(100)),
            sa.Column("booking", sa.Boolean()),
            sa.Column("sources", _JSON),
            sa.Column("extra", _JSON),
            sa.Column("origin", sa.String(12), nullable=False, server_default="local"),
            sa.Column("agent", sa.String(128)),
            sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("edited", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("activity_id", sa.String(36)),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        for col in ("finding_id", "trip_id", "user_id"):
            op.create_index(f"ix_discovery_items_{col}", "discovery_items", [col])
    _backfill()


def _backfill() -> None:
    from discover.items import _dedupe, rows_from_finding

    bind = op.get_bind()
    typed = {"sources": _JSON, "extra": _JSON, "price_tier": sa.Integer(),
             "hidden": sa.Boolean(), "edited": sa.Boolean(),
             "created_at": sa.DateTime(timezone=True), "updated_at": sa.DateTime(timezone=True)}
    items = sa.table("discovery_items", *[sa.column(c, typed.get(c, sa.String())) for c in (
        "id", "finding_id", "trip_id", "user_id", "topic", "place_key", "kind", "name",
        "neighbourhood", "address", "website", "what_to_get", "price_tier", "starts_on",
        "ends_on", "when_text", "venue", "details", "sources", "extra", "origin",
        "hidden", "edited", "created_at", "updated_at")])
    have = {r[0] for r in bind.execute(sa.text("SELECT DISTINCT finding_id FROM discovery_items"))}
    now = datetime.now(timezone.utc)
    rows = bind.execute(sa.text(
        "SELECT id, trip_id, user_id, topic, place_key, data FROM discovery_findings "
        "WHERE topic IN ('food', 'events')")).fetchall()
    out = []
    for fid, trip_id, user_id, topic, place_key, data in rows:
        if fid in have:
            continue
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                continue
        for r in _dedupe(rows_from_finding(topic, data)):
            out.append({"id": str(uuid.uuid4()), "finding_id": fid, "trip_id": trip_id,
                        "user_id": user_id, "topic": topic, "place_key": place_key,
                        "kind": r["kind"], "name": r["name"],
                        "neighbourhood": r.get("neighbourhood"), "address": r.get("address"),
                        "website": r.get("website"), "what_to_get": r.get("what_to_get"),
                        "price_tier": r.get("price_tier"), "starts_on": r.get("starts_on"),
                        "ends_on": r.get("ends_on"), "when_text": r.get("when_text"),
                        "venue": r.get("venue"), "details": r.get("details"),
                        "sources": r.get("sources"), "extra": r.get("extra"),
                        "origin": "local", "hidden": False, "edited": False,
                        "created_at": now, "updated_at": now})
    if out:
        op.bulk_insert(items, out)


def downgrade() -> None:
    op.drop_table("discovery_items")
