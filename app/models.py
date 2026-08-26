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


def _slug(text: str | None) -> str:
    """A stable snake_case key from a label — "Car Phone Mount" -> "car_phone_mount"
    — matching the seed catalog's id convention. Falls back to a uuid fragment when
    the text has no usable characters."""
    import re
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return s[:64] or ("item_" + _uuid()[:8])


def _slug_from_label(ctx) -> str:
    """Column default: derive `id` from the row's `label` on insert (used by
    PackingCatalogItem, whose PK is a slug the owner shouldn't have to type)."""
    return _slug(ctx.get_current_parameters().get("label"))


def _phrase_id(ctx) -> str:
    """PhrasebookPhrase PK on insert: `{lang}:{key}` (one phrase per lang+key)."""
    p = ctx.get_current_parameters()
    return f"{(p.get('lang_code') or '').strip().lower()}:{(p.get('phrase_key') or '').strip()}"


def _region_lang_id(ctx) -> str:
    """RegionalLanguage PK on insert: `{country}:{region}:{lang}`."""
    p = ctx.get_current_parameters()
    return (f"{(p.get('country') or '').strip().upper()}:"
            f"{(p.get('region') or '').strip().lower()}:"
            f"{(p.get('lang_code') or '').strip().lower()}")


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


class User(Base):
    """A human account (Yonder 4.0 Phase 1a, SPEC-4 D-111). Yonder had no users
    before 4.0; this is the identity all of API/MCP/web authenticate as.

    Dual-source (D-111): `auth_source` is local | oidc | proxy. Local carries a
    scrypt `password_hash`; oidc/proxy carry `external_subject` and a null hash.
    `token_version` is the revocation lever — bumping it invalidates every
    outstanding access/web/refresh token for this user ("sign out everywhere").
    `traveler_id` (D-112) links the account to the existing self/guest roster
    (TravelerIdentity) so there is no parallel identity concept. Per-user data
    isolation is a LATER phase (2); Phase 1 only establishes who you are.
    """
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(255))  # null for oidc/proxy
    auth_source: Mapped[str] = mapped_column(String(16), default="local")  # local|oidc|proxy
    external_subject: Mapped[str | None] = mapped_column(String(255), index=True)  # oidc sub / proxy:<name>
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    traveler_id: Mapped[int | None] = mapped_column(Integer, index=True)  # -> traveler_identities.id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshToken(Base):
    """A rotating refresh token for the mobile/API flow (SPEC-4 D-111). The
    opaque secret is shown to the client once; only its sha256 is stored, so a
    DB leak cannot mint sessions. Consumed-and-rotated on every refresh; a
    token is revocable individually (revoked_at) or en masse via User.token_version.
    Web sessions use a JWT cookie instead and don't create rows here."""
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(256))


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
    # 3.2 (D-94): soft link to the person this home belongs to. NULL = the
    # deployment-wide default (backward compatible — the `self` traveller inherits
    # all NULL homes until a UI assigns them). Integer to match traveler_identities.id.
    traveler_id: Mapped[int | None] = mapped_column(Integer, index=True)
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
    # 3.2 (D-91): ISO-2 country code, used by the visa resolver (the `self`
    # traveller's nationality). NULL = visa research skipped with a visible prompt.
    nationality: Mapped[str | None] = mapped_column(String(2))
    # 3.2 (D-100): a generic guest is a roster placeholder — "someone is coming"
    # — NOT a modelled person. Minted by the trip "add a guest" quick-add. It
    # carries no profile/nationality and is deliberately EXCLUDED from discovery
    # and planning (see routes.discovery_travelers), so guest records never become
    # a dumping ground for considerations ("female guest", "old guest"). To make a
    # companion actually count, give them a real profile instead of a guest name.
    generic: Mapped[bool] = mapped_column(Boolean, default=False,
                                          server_default="0")
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


class TripTraveler(Base):
    """Yonder 3.2 (D-94) — who is on a trip. A structured association replacing
    the old free-text `traveling_with` hint: each row links a trip to a
    TravelerIdentity with a role. Discovery reads the roster to look up each
    person's DiscoveryProfile/PackingProfile and the `self` traveller's
    nationality (visa resolver). `traveling_with` is now derived from this
    roster (D-96), not entered on a profile."""
    __tablename__ = "trip_travelers"

    trip_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trips.id", ondelete="CASCADE"), primary_key=True)
    traveler_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="companion")  # self|companion


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


class DiscoveryRun(Base):
    """Yonder 3.0 — one Discovery research pass over a Trip (D-63). Unlike the
    in-memory review job registry (app/review/jobs.py), a Discovery run is a
    DURABLE row: research is quality-paced and minutes-to-hours long, and its
    output must survive a container restart. The background thread writes
    phase/done/total here; the poll panel reads them. A run orphaned by a
    restart is marked `failed` on next poll rather than vanishing.

    `params` snapshots what the run was launched against — the topic set and the
    derived place set (D-62) — so the run is reproducible and auditable after the
    trip's activities later change."""
    __tablename__ = "discovery_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Soft link, plain indexed column (same convention as travel_items.trip_id —
    # no DB FK; the trips delete route is responsible for cleanup).
    trip_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(12), default="running", index=True)  # running|done|failed|cancelled
    phase: Mapped[str | None] = mapped_column(String(128))
    done: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    params: Mapped[dict | None] = mapped_column(JsonCol)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DiscoveryFinding(Base):
    """Yonder 3.0 — one researched topic for one place, held on Yonder for human
    review/edit/approve BEFORE it is pushed to AdventureLog (D-63). Follows the
    TravelItem envelope philosophy: the rendered `body_md` is what crosses to an
    AdventureLog Note; structured `data` (currency rates, a weather series) rides
    alongside for rich in-Yonder rendering (e.g. the currency calculator, D-65).

    Idempotency is on (trip_id, topic, place_key), enforced in app code — a re-run
    or a steerable single-topic re-run (D-71) upserts in place instead of
    duplicating (same app-enforced-uniqueness pattern as trip_activities, 0009).
    `steer_note` records the operator's correction fed into the resolver prompt on
    a re-run, for provenance."""
    __tablename__ = "discovery_findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    trip_id: Mapped[str] = mapped_column(String(36), index=True)
    topic: Mapped[str] = mapped_column(String(24), index=True)  # weather|currency|visa|...
    scope: Mapped[str] = mapped_column(String(8))               # country|city
    place_key: Mapped[str] = mapped_column(String(128))          # "US" | "US:Glendale"
    status: Mapped[str] = mapped_column(String(12), default="draft")  # draft|approved|edited|rejected
    body_md: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JsonCol)
    sources: Mapped[list | None] = mapped_column(JsonCol)
    confidence: Mapped[str | None] = mapped_column(String(8))    # high|medium|flagged
    resolver: Mapped[str | None] = mapped_column(String(32))
    steer_note: Mapped[str | None] = mapped_column(Text)
    ttl_seconds: Mapped[int | None] = mapped_column(Integer)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    adventurelog_note_id: Mapped[str | None] = mapped_column(String(64))


class PackingProfile(Base):
    """Yonder 3.1 — the "ask once" packing preferences (SPEC-3.1-planning.md §3).

    Real preferences that can't be derived from a trip — bag setup, laundry
    comfort, climate tolerance, style constraints, must-haves, clothing variant.
    Scaffolded per-traveller via `traveler_id` (D-82), but Yonder has no multi-user
    yet, so 3.1 uses the single `self` traveller. Preferences live in `values` as a
    JSON blob — same convention as TripActivity.values — so adding a preference is
    a data change, not a migration. Managed in the admin UI like HomeProfile /
    TravelerIdentity (never hardcoded)."""
    __tablename__ = "packing_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Soft link to a TravelerIdentity (plain column, house convention). NULL = the
    # deployment-wide default profile used while Yonder is single-user. 3.2 (D-94):
    # Integer to match traveler_identities.id — migration 0014 converts the legacy
    # String(36) column (always NULL to date) to the coherent type.
    traveler_id: Mapped[int | None] = mapped_column(Integer, index=True)
    values: Mapped[dict] = mapped_column(JsonCol, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class DiscoveryProfile(Base):
    """Yonder 3.2 (D-89) — the "ask once" discovery preferences, peer of
    PackingProfile and following the same proven pattern: nullable `traveler_id`
    soft-linked to TravelerIdentity (NULL = deployment-wide default while
    single-user), preferences in a `values` JSON blob so adding a field is a data
    change, never a migration (D-82). Exactly one per person (D-95).

    Constraint-first (D-87): the blob captures what to SKIP (dietary restrictions,
    cuisine dislikes, budget ceiling, mobility) and what to ADD (drink/activity
    interests) — never affinities inferred from origin or history (D-93). The
    resolvers read the sections they care about and shape their SearXNG queries
    and Ollama prompts accordingly. A blank profile means "show everything"."""
    __tablename__ = "discovery_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Soft link to a TravelerIdentity (house convention, Integer to match the PK).
    # NULL = the deployment-wide default profile used while Yonder is single-user.
    traveler_id: Mapped[int | None] = mapped_column(Integer, index=True)
    values: Mapped[dict] = mapped_column(JsonCol, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class PackingPlan(Base):
    """Yonder 3.1 — one generated Packing Plan for a trip (peer of DiscoveryRun).

    The plan is an aggregation of the deterministic engine's passes: it records the
    chosen archetypes + scores, the derived context snapshot, the pre-flight
    warnings, and the IN-TRIP tasks the packing math implies (laundry cadence,
    etc. — D-83). The checklist lines themselves are PackingItem rows."""
    __tablename__ = "packing_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    trip_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(12), default="running", index=True)
    archetypes: Mapped[dict | None] = mapped_column(JsonCol)   # chosen + scores
    # 3.2.1: the operator's explicit archetype choice (list of ids), overriding
    # auto-detection — Banff can be ski OR city depending on the trip, so the guess
    # is confirmable/adjustable. NULL/empty = auto-detect; carried forward across
    # plain re-generates so the choice sticks.
    manual_archetypes: Mapped[list | None] = mapped_column(JsonCol)
    context: Mapped[dict | None] = mapped_column(JsonCol)      # derived inputs snapshot
    warnings: Mapped[list | None] = mapped_column(JsonCol)
    tasks: Mapped[list | None] = mapped_column(JsonCol)        # in-trip actions
    error: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PackingItem(Base):
    """Yonder 3.1 — one checklist line of a Packing Plan (peer of DiscoveryFinding).

    Idempotent on (trip_id, item_key): a re-generate upserts in place and PRESERVES
    the human states (packed = ticked off, removed = you dropped it), refreshing
    only draft rows — the same review-survives-regeneration rule as Discovery
    (D-63). `omitted` is the engine's own "left behind" decision (with a reason and
    optional swap) — shown, never hidden (D-83)."""
    __tablename__ = "packing_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    plan_id: Mapped[str | None] = mapped_column(String(36), index=True)
    trip_id: Mapped[str] = mapped_column(String(36), index=True)
    item_key: Mapped[str] = mapped_column(String(64), index=True)   # "clothing:quick_dry_top"
    label: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # carry_on | checked | backpack (the engine's generic guess), OR a specific
    # Bag.id once the owner has a bag inventory and assigns one to this line —
    # single column, widened (was 12) to hold a Bag slug id alongside the
    # generic values (see Bag below).
    bag: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(256))
    source: Mapped[str | None] = mapped_column(String(16))          # template|modifier|must_have|...
    swap_for: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(12), default="draft")  # draft|packed|removed|omitted
    # A human hand-edit (quantity/bag/label/a fully custom line) — orthogonal to
    # `status`. Once true, a re-generate never overwrites or prunes this row,
    # the same durability `packed`/`removed` already get (D-63 review-survives-
    # regeneration), so e.g. a quantity tweak on a still-"draft" row sticks.
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class PackingCatalogItem(Base):
    """Yonder 3.1 — the packing item catalog as EDITABLE data (D-84).

    Seeded from planning/data/items.json at startup — JSON is the shipped DEFAULT,
    exactly as RuleSetting seeds from the rules registry (new items in a release
    appear; existing rows are left alone so owner edits survive). Thereafter the DB
    is truth and the owner manages items through the admin UI, never by editing
    JSON. The Planning engine reads its item catalog from here.

    `id` is the stable item key referenced by archetypes.json ("quick_dry_top").
    `enabled` lets an item be retired without deleting it (and orphaning an
    archetype reference)."""
    __tablename__ = "packing_catalog_items"

    # Owner-facing PK is a slug referenced by archetypes.json. The seed sets it
    # from items.json keys; a NEW item added in the admin auto-derives it from the
    # label (SQLAdmin drops the PK from the create form, so it would otherwise
    # insert NULL — the D-84 catalog-add bug).
    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=_slug_from_label)
    label: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(16), index=True)
    bag: Mapped[str] = mapped_column(String(12), default="carry_on")   # carry_on|checked
    weight_g: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[list | None] = mapped_column(JsonCol, default=list)
    variants: Mapped[dict | None] = mapped_column(JsonCol, default=dict)  # {masc,fem,neutral}
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class Bag(Base):
    """Yonder 3.9.2 — the owner's actual bag inventory (the 3 backpacks, the
    carry-on, the checked suitcase — with real capacities), parallel to and
    independent from what goes IN a bag (PackingItem/PackingCatalogItem). Not
    the Packing Profile (that's trip-preference config, D-73..D-85) — this is
    a plain inventory, managed in the admin like PackingCatalogItem (D-84).

    `id` is a slug from the label (same convention as PackingCatalogItem) so a
    PackingItem can soft-link to a specific bag via its own `bag` column
    (generic carry_on/checked/backpack until the owner has bags to assign,
    then a real Bag.id) without a second column."""
    __tablename__ = "bags"

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=_slug_from_label)
    label: Mapped[str] = mapped_column(String(64))          # "Osprey Farpoint 40"
    kind: Mapped[str] = mapped_column(String(16), default="backpack",
                                      index=True)            # backpack|suitcase|personal_item
    capacity: Mapped[str | None] = mapped_column(String(32))  # "40L", "22in carry-on", freeform
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class PhrasebookPhrase(Base):
    """Yonder 3.6 — the traveller phrasebook as EDITABLE data.

    Seeded from discover/data/phrases.json at startup (JSON = shipped default,
    same idempotent contract as the packing catalog); thereafter the DB is truth
    and the owner edits phrases in the admin. ONE row per (language, phrase key).

    `source` distinguishes the human-VETTED skeleton (L2) from LLM-GENERATED
    fallbacks — regional languages the vetted set lacks (Kannada, etc.). Generated
    rows are PERSISTED so (a) the owner can review and PROMOTE them to `vetted`
    with one edit, turning the fallback into a curation pipeline, and (b) a re-run
    REUSES them instead of regenerating. Resolvers render generated phrases under a
    flag; a vetted row is rendered plain."""
    __tablename__ = "phrasebook_phrases"

    # PK is `{lang}:{key}` (e.g. "de:hello"); auto-derived on an admin create,
    # since SQLAdmin drops the PK from the create form (would insert NULL).
    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_phrase_id)
    lang_code: Mapped[str] = mapped_column(String(12), index=True)
    phrase_key: Mapped[str] = mapped_column(String(24))
    text: Mapped[str] = mapped_column(String(200))
    pronunciation: Mapped[str | None] = mapped_column(String(200), default="")
    source: Mapped[str] = mapped_column(String(12), default="vetted")   # vetted|generated
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (UniqueConstraint("lang_code", "phrase_key",
                                       name="uq_phrasebook_lang_key"),)


class RegionalLanguage(Base):
    """Yonder 3.6 — the region→language map as EDITABLE data.

    Seeded from discover/data/regional_languages.json at startup; thereafter
    owner-edited in the admin. ONE row per (country, region, language). The
    regional-language resolver surfaces these for a city whose geocoded region
    matches, BEYOND the national official set (Karnataka→Kannada, etc.). `region`
    is stored as entered and lower-cased at read time, so the owner types
    "Karnataka", not "karnataka"."""
    __tablename__ = "regional_languages"

    id: Mapped[str] = mapped_column(String(80), primary_key=True,
                                    default=_region_lang_id)
    country: Mapped[str] = mapped_column(String(2), index=True)
    region: Mapped[str] = mapped_column(String(64))
    lang_code: Mapped[str] = mapped_column(String(12))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (UniqueConstraint("country", "region", "lang_code",
                                       name="uq_regional_lang"),)


class DiscoveryExclusion(Base):
    """A derived place the operator chose NOT to research for a trip — a layover
    city they only pass through, a mis-geocoded stop (SPEC-3-discovery.md §1).

    `place_key` is the DerivedPlace identity shown in the "What Yonder found"
    viewer ("DE:Frankfurt" for a city, "DE" for a country-only place). A run
    filters excluded places out of the derived set before planning; the toggle in
    the viewer adds/removes rows here. Plain soft link on trip_id, app-enforced
    uniqueness on (trip_id, place_key) — same convention as the other tables."""
    __tablename__ = "discovery_exclusions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    trip_id: Mapped[str] = mapped_column(String(36), index=True)
    place_key: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
