"""Phase 0 data model (HANDOFF Task 5, amended by D-24/D-25):
travel_items, item_keys (A5), ingest_batches, commit_attempts, source_rules,
api_keys, artifacts, home_profiles, rule_settings.
`collection_hints` is deliberately ABSENT — cut from MVP (D-24).

JSON columns become JSONB on Postgres, plain JSON on SQLite (tests).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, JSON,
                        String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonCol = JSON().with_variant(JSONB(), "postgresql")


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TravelItem(Base):
    """The one state table (HANDOFF pipeline). The envelope sub-objects stay
    JSON: the canonical schema is the contract, jsonschema+rules.py the gate —
    mirroring them as columns would create a second, drifting schema."""
    __tablename__ = "travel_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    batch_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("ingest_batches.id"), index=True)
    # A5 identity; null until resolution. NOT unique: several rows legitimately
    # share one key over time — a recapture (no_change), an update, a
    # cancellation superseding its confirmation — chained via
    # revision.prior_item_id (D-13) and kept forever (D-15). Uniqueness of
    # identity lives in item_keys.key, which maps a key to one owning item.
    canonical_key: Mapped[str | None] = mapped_column(String(512), index=True)
    item_type: Mapped[str | None] = mapped_column(String(32), index=True)
    segment_index: Mapped[int | None] = mapped_column(Integer)
    segment_count: Mapped[int | None] = mapped_column(Integer)
    booking_group_id: Mapped[str | None] = mapped_column(String(64), index=True)
    schema_version: Mapped[str | None] = mapped_column(String(16))
    artifact_sha256: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("artifacts.sha256"))
    # Yonder 2.1.2: soft link to an in-house Yonder Trip, set by the review trip
    # picker. Independent of the AdventureLog collection (resolution.collection_id)
    # — Yonder trips are their own trip system, not a mirror. Plain column, no DB
    # FK (see migration 0008); the trips delete route clears dangling links.
    trip_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[dict | None] = mapped_column(JsonCol)
    source: Mapped[dict | None] = mapped_column(JsonCol)
    extraction: Mapped[dict | None] = mapped_column(JsonCol)
    resolution: Mapped[dict | None] = mapped_column(JsonCol)
    revision: Mapped[dict | None] = mapped_column(JsonCol)
    review: Mapped[dict | None] = mapped_column(JsonCol)
    commit: Mapped[dict | None] = mapped_column("commit_data", JsonCol)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class ItemKey(Base):
    """A5 key set: primary + alternates. Matching = lookup on incoming keys."""
    __tablename__ = "item_keys"
    __table_args__ = (UniqueConstraint("key", name="uq_item_keys_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("travel_items.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(512), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="alternate")  # primary|alternate


class IngestBatch(Base):
    """Raw accepted /ingest payloads. 202 means 'stored', never 'processed' —
    processing happens async so an AdventureLog failure can never force
    re-parsing an email (HANDOFF pipeline invariant)."""
    __tablename__ = "ingest_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    api_key_id: Mapped[str] = mapped_column(String(36), ForeignKey("api_keys.id"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    declared_segment_count: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JsonCol)
    artifact_sha256: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("artifacts.sha256"))
    status: Mapped[str] = mapped_column(String(16), default="received", index=True)


class CommitAttempt(Base):
    """Verbatim HTTP exchange per write attempt (D-19). Meaning often lives
    only in the raw response body."""
    __tablename__ = "commit_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("travel_items.id", ondelete="CASCADE"), index=True)
    target: Mapped[str] = mapped_column(String(64))     # airtrail.flight, adventurelog.location:YYZ ...
    operation: Mapped[str | None] = mapped_column(String(16))
    request_payload: Mapped[dict | None] = mapped_column(JsonCol)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_headers: Mapped[dict | None] = mapped_column(JsonCol)
    response_body: Mapped[str | None] = mapped_column(Text)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SourceRule(Base):
    """Ingestion routing rules as data (D-17). NOTE: collection_hints was cut
    from MVP (D-24); source_rules stands."""
    __tablename__ = "source_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    match: Mapped[dict] = mapped_column(JsonCol)        # e.g. {"label": "Travel"}
    action: Mapped[dict] = mapped_column(JsonCol)       # e.g. {"item_type_hint": "flight"}
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)


class ApiKey(Base):
    """Per-client revocable ingest keys (Karakeep model). Only the sha256 of
    the key is stored; the plaintext is shown once at creation."""
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    client_name: Mapped[str] = mapped_column(String(128), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Artifact(Base):
    """Immutable content-addressed originals (D-15, Paperless pattern).
    Bytes live on disk under artifacts_dir/<sha256>; this row is the index."""
    __tablename__ = "artifacts"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    storage_path: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class HomeProfile(Base):
    """Effective-dated home config (D-07): a 2013 flight is judged against the
    home in force then. Bangalore must NOT be seeded here (OQ-01)."""
    __tablename__ = "home_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))       # residence|home_airport|home_region
    label: Mapped[str] = mapped_column(String(128))
    # Airport/metro IATA for kind=home_airport. Matching also understands metro
    # codes (YTO covers YYZ+YTZ) — see resolve/home.py METRO_GROUPS.
    iata: Mapped[str | None] = mapped_column(String(3))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    radius_km: Mapped[float] = mapped_column(Float, default=50.0)   # OQ-02: tune
    country: Mapped[str | None] = mapped_column(String(2))
    effective_from: Mapped[str | None] = mapped_column(String(10))  # ISO date
    effective_to: Mapped[str | None] = mapped_column(String(10))
    notes: Mapped[str | None] = mapped_column(String(512))


class TravelerIdentity(Base):
    """Who the deployment's traveller are (D-22/RULE-10).

    AirTrail only holds the account holder's own flight history, so exactly one
    row is kind="self" — the person matching the AirTrail user — and everyone
    else is a guest. Names on tickets vary wildly ("OKAFOR/DANIEL MR",
    "R OKAFOR"), so each identity carries aliases; matching is on normalised
    token sets (resolve/identity.py). Per-deployment personal config, managed
    in the admin UI, never hardcoded (same principle as HomeProfile).
    """
    __tablename__ = "traveler_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(8), default="guest")   # self | guest
    aliases: Mapped[list | None] = mapped_column(JsonCol, default=list)
    airtrail_user_id: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(String(512))


class Trip(Base):
    """Yonder 2.0 — an in-house trip. The first capability Yonder owns outright:
    trips no longer live only as AdventureLog collections. A trip is a named
    span with optional dates and free-text notes; its day-by-day contents are
    TripActivity rows.

    `source`/`external_ref` are forward hooks for 2.1 seeding (AirTrail first,
    then AdventureLog): `source` records where a seeded trip came from and
    `external_ref` the originating id, so a re-seed dedups instead of
    duplicating. A hand-made trip is source="manual" with no external_ref.
    source="resolver" (2.2.2) is minted by the commit stage alongside a NEW
    AdventureLog collection, when a flight closes a home-to-home journey that
    has no existing Yonder trip to auto-link to — the two systems should never
    disagree on whether a trip exists.
    """
    __tablename__ = "trips"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    # ISO dates as strings (same style as HomeProfile.effective_from) — a trip
    # is planned in whole days, never to the second, and string dates sort and
    # compare correctly without a tz to reason about.
    start_date: Mapped[str | None] = mapped_column(String(10))
    end_date: Mapped[str | None] = mapped_column(String(10))
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="manual")   # manual|airtrail|adventurelog|resolver
    external_ref: Mapped[str | None] = mapped_column(String(128), index=True)
    # AdventureLog link (2.1): the collection this trip maps to. Kept separate
    # from external_ref (seed provenance) so a trip seeded FROM AirTrail can
    # still carry an AdventureLog collection id. Set by "Push to AdventureLog"
    # or entered by hand; the detail page links to it.
    adventurelog_collection_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class TripActivity(Base):
    """One entry on a trip's timeline — a flight, a hotel night, a dinner, a
    note. The type-specific fields stay in one `values` JSON blob rather than
    columns: the activity-type registry (app/trips/types.py) is the contract for
    what each type carries, mirroring how TravelItem keeps its envelope in JSON
    instead of a second, drifting column set.

    `starts_at` is a denormalized copy of values["when"] kept only so the
    timeline can order and day-group in the database; an activity with no date
    stores "" and sorts last ("Date not set")."""
    __tablename__ = "trip_activities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    trip_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(24))
    starts_at: Mapped[str | None] = mapped_column(String(32), index=True)
    values: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # Yonder 2.2.0: the canonical_key of the capture this activity was logged
    # from, or NULL for a hand-made one. The commit stage upserts on
    # (trip_id, source_item_key) so a re-committed / re-captured booking updates
    # in place instead of duplicating (migration 0009).
    source_item_key: Mapped[str | None] = mapped_column(String(512), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class RuleSetting(Base):
    """D-25: rule logic is code (rules.py registry), rule ENABLEMENT is data.
    Seeded from the registry at startup; SQLAdmin over this table is the
    management UI. mode: enforce | audit | off."""
    __tablename__ = "rule_settings"

    rule_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default="enforce")
    note: Mapped[str | None] = mapped_column(String(512))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)
