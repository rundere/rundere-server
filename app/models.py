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
