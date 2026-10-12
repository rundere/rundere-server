"""Phase 0 data model (HANDOFF Task 5, amended by D-24/D-25):
travel_items, item_keys (A5), ingest_batches, commit_attempts, source_rules,
api_keys, artifacts, home_profiles, rule_settings.
`collection_hints` is deliberately ABSENT — cut from MVP (D-24).

JSON columns become JSONB on Postgres, plain JSON on SQLite (tests).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON,
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
    # 4.0 Phase 2 (D-120): the submitting user, inherited from the ingest batch
    # (key.user_id) or set from the /capture/web proxy user. NULL = open/legacy;
    # the review queue is scoped to owner_id (own + open). See app/scope.py.
    owner_id: Mapped[str | None] = mapped_column(String(36), index=True)
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
    # 4.0 Phase 2 (D-120): the submitting user, copied from the ingesting key's
    # user_id (or the /capture/web proxy user). Propagated onto each TravelItem's
    # owner_id. NULL = an unscoped/legacy key → open capture.
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
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
    # 4.0 Phase 2 (D-120): scope this ingest key to a user, so captures it
    # submits are owned by them (each household member runs their own Apps Script
    # with their own key). NULL = unscoped/legacy key: refused by /ingest while
    # sign-in is on (YONDER-165); with it off, captures are ownerless as before.
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
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
    # 4.1 admin sharing (migration 0027). ON (default): admins can view and edit
    # this user's data. OFF (opted out): admins see only what the user invites
    # them into, like any other user. A UI policy, not encryption — the server
    # operator can always read the database. Opting out needs SMTP + an email so
    # a password reset never has to pass through an admin.
    share_with_admins: Mapped[bool] = mapped_column(Boolean, default=True,
                                                    server_default="1")
    # Self-registered / SSO-provisioned accounts are asked to choose at their
    # first sign-in; accounts an admin creates share by default, unasked.
    sharing_prompt: Mapped[bool] = mapped_column(Boolean, default=False,
                                                 server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountEvent(Base):
    """4.1 audit trail of account actions taken by someone OTHER than the account
    holder (an admin resetting a password, changing a role...) plus sharing
    changes. The account holder is emailed about admin actions when they've
    opted out of sharing. Append-only; soft links (house convention)."""
    __tablename__ = "account_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)      # the account
    actor_id: Mapped[str | None] = mapped_column(String(36))          # who did it (None = CLI)
    action: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(String(255))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


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


class OAuthClient(Base):
    """YONDER-107: an OAuth 2.1 client (claude.ai, Claude Code, the iOS app).
    Registered dynamically (RFC 7591, `source="dcr"`) or pre-registered with
    `app.cli oauth-client create` (`source="cli"`). Registering grants nothing by
    itself — every user still signs in and consents. `secret_hash` (sha256) is
    set only for confidential clients; public clients rely on PKCE."""
    __tablename__ = "oauth_clients"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    redirect_uris: Mapped[list] = mapped_column(JsonCol)
    secret_hash: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(8), default="dcr")      # dcr | cli
    created_by: Mapped[str | None] = mapped_column(String(36))         # user id (None = DCR/CLI)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OAuthCode(Base):
    """A single-use authorization code (≤60 s), bound to client + redirect_uri +
    PKCE challenge + user. Only its sha256 is stored. `grant_id` is filled when
    it's exchanged, so a replayed code can revoke the grant it produced."""
    __tablename__ = "oauth_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    redirect_uri: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(String(128))
    scope: Mapped[str | None] = mapped_column(String(255))
    resource: Mapped[str | None] = mapped_column(String(512))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    grant_id: Mapped[str | None] = mapped_column(String(36))


class OAuthGrant(Base):
    """One user's consent to one client — what Account → Connected apps lists.
    Holds the CURRENT rotating refresh token's sha256; `prev_refresh_hashes`
    keeps recently retired ones so a replayed (stolen) refresh token is
    recognised and revokes the whole grant. Sliding expiry: every refresh
    pushes `expires_at` out. `token_version` snapshots User.token_version so
    "sign out everywhere" also ends the grant."""
    __tablename__ = "oauth_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    scope: Mapped[str | None] = mapped_column(String(255))
    resource: Mapped[str | None] = mapped_column(String(512))
    refresh_hash: Mapped[str | None] = mapped_column(String(64))
    prev_refresh_hashes: Mapped[list | None] = mapped_column(JsonCol)
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PersonalApiToken(Base):
    """A personal API token (`ypat_…`) for non-OAuth clients (Claude Code, the
    stdio MCP script). Created and revoked on the Account page, shown once,
    stored as sha256. Optional expiry; `token_version` snapshot as for grants."""
    __tablename__ = "personal_api_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(128))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordResetToken(Base):
    """One-time password-reset link (Phase 1a.1, D-117). Only the sha256 of the
    opaque token is stored; the link carries the plaintext. Single-use (used_at)
    and time-boxed (expires_at). Emailed when SMTP is configured, otherwise an
    admin generates and hands over the link from the Users page."""
    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    # 4.0 (D-65): the traveller's own base currency — the one the discovery
    # currency resolver + calculator convert FROM on their trips. An EXPLICIT
    # profile choice (ISO-4217, e.g. "GBP"), independent of nationality and home.
    # NULL = fall back to the deployment default (TX_DISCOVERY_BASE_CURRENCIES).
    base_currency: Mapped[str | None] = mapped_column(String(3))
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
    # 4.0 Phase 2 (D-121): the owning user. NULL = open/legacy (visible to all);
    # a manual trip is owned by its creator; a resolver-minted trip stays NULL
    # (D-119, no user context). Sharing is via TripMember. See app/scope.py.
    owner_id: Mapped[str | None] = mapped_column(String(36), index=True)
    # SPEC-10 (D-176): the ISO-4217 currency the trip's expense balances are shown
    # in. NULL = not chosen yet; the first expense's currency is used and saved.
    expense_currency: Mapped[str | None] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class TripMember(Base):
    """4.0 Phase 2 (D-121): a user a trip is shared with. v1 = "member = full
    access" (view + edit); `role` reserves viewer/editor for later. The owner
    isn't listed here — ownership is `Trip.owner_id`."""
    __tablename__ = "trip_members"
    __table_args__ = (UniqueConstraint("trip_id", "user_id", name="uq_trip_member"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    trip_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(16), default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


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
    # 4.2.x: where it is — located points (+ a road trip's route line), derived
    # from `values` on save or from a capture's payload (app/trips/geo.py,
    # migration 0029). NULL = not located yet.
    geo: Mapped[dict | None] = mapped_column(JsonCol, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class TripActivityTraveler(Base):
    """4.1 — which of the trip's travellers an activity is for (migration 0025).
    NO rows for an activity = everyone on the trip, the default, so a solo trip
    or a shared dinner never needs tagging. Only ids on the trip's roster are
    ever stored (app/trips/who.py); removing someone from the roster or deleting
    the activity removes their rows. Soft links, no DB FK."""
    __tablename__ = "trip_activity_travelers"

    activity_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    traveler_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)


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
    # 4.1: Discover is per person (migration 0026) — whose run/finding/exclusion
    # this is. NULL = auth-off single-user mode (same rule as packing, D-123).
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
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
    # 4.1: Discover is per person (migration 0026) — whose run/finding/exclusion
    # this is. NULL = auth-off single-user mode (same rule as packing, D-123).
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
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


class DiscoveryItem(Base):
    """Run·dere 4.5 — one place, event or activity inside a Food & Drink or
    Events & Activities finding (Discovery 2.0, YONDER-147).

    A finding is the city's write-up; its items are the rows people sort, hide
    and add to the timeline, so each needs its own identity. Items belong to a
    finding and are re-synced whenever that finding is regenerated
    (discover/items.py): matched by name, a row keeps what the person did with
    it (hidden, edited, added to the timeline); rows the new result no longer
    has are dropped unless the person touched them.

    `kind` is the fine type — restaurant/cafe/bar/pub/brewery/… for food,
    `event` or `activity` for events. Dates are ISO strings like the rest of
    the schema; `when_text` keeps the source's own wording ("12–14 Oct")."""
    __tablename__ = "discovery_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    finding_id: Mapped[str] = mapped_column(String(36), index=True)
    trip_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
    topic: Mapped[str] = mapped_column(String(24))               # food | events
    place_key: Mapped[str] = mapped_column(String(128))          # the finding's city key
    kind: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(300))
    neighbourhood: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(500))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    website: Mapped[str | None] = mapped_column(String(2000))
    what_to_get: Mapped[str | None] = mapped_column(Text)       # food: the dish/drink to order
    price_tier: Mapped[int | None] = mapped_column(Integer)      # food: 1–4
    starts_on: Mapped[str | None] = mapped_column(String(16))    # event: YYYY-MM-DD[THH:MM]
    ends_on: Mapped[str | None] = mapped_column(String(16))
    when_text: Mapped[str | None] = mapped_column(String(200))
    venue: Mapped[str | None] = mapped_column(String(300))
    details: Mapped[str | None] = mapped_column(Text)            # why go / what it is
    duration: Mapped[str | None] = mapped_column(String(100))    # activity
    booking: Mapped[bool | None] = mapped_column(Boolean)        # activity/food: book ahead
    sources: Mapped[list | None] = mapped_column(JsonCol)
    extra: Mapped[dict | None] = mapped_column(JsonCol)          # anything else the source had
    origin: Mapped[str] = mapped_column(String(12), default="local")   # local | agent
    agent: Mapped[str | None] = mapped_column(String(128))
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    hidden_reason: Mapped[str | None] = mapped_column(String(16))  # not_my_thing | closed | been_there
    edited: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    activity_id: Mapped[str | None] = mapped_column(String(36))  # added to the timeline as
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


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
    # 4.0 Phase 2 (D-123): packing is PER-USER — a plan belongs to the viewing
    # user, keyed by (trip_id, user_id). No cross-visibility between travellers.
    # NULL = legacy/auth-off shared plan. Discovery, by contrast, stays trip-wide.
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
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
    # 4.0 Phase 2 (D-123): per-user, like its PackingPlan. Idempotency is now on
    # (trip_id, user_id, item_key). NULL = legacy/auth-off.
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
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
    # 4.1: Discover is per person (migration 0026) — whose run/finding/exclusion
    # this is. NULL = auth-off single-user mode (same rule as packing, D-123).
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
    place_key: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class JournalEntry(Base):
    """Yonder 4.x — a travel journal entry (SPEC-6). A quick, first-person, dated
    note the traveller AUTHORS — the counter-weight to the rest of Yonder, which
    is captured structured data run through extract→resolve→commit. A journal
    entry is deliberately NOT a TravelItem or a TripActivity (D-131): it never
    touches that pipeline or the rules engine, carries no type contract, and is
    strictly personal (own-only scope, D-134) — sharing a trip never exposes a
    co-traveller's journal.

    Organising axis is `entry_date`, the day the entry is ABOUT — you write up
    yesterday's hike over breakfast (D-132). `captured_at` keeps the true device
    timestamp for provenance and intra-day ordering, and is the minimum "time
    context" every entry carries alongside `entry_date` (D-142). Geo
    (`place_lat`/`place_lon`, later `place_label`) is best-effort — captured
    automatically on mobile when permitted, opportunistic in the browser, never
    required, and never blocks a save.

    Standalone-capable: `trip_id` is nullable and set later by an explicit
    attach action (D-133); a field note can precede any trip. An entry belongs to
    exactly ONE trip at a time — to use the same note against another trip the
    author DUPLICATES it (`duplicated_from` records the fork) and attaches the
    copy. Soft link on trip_id (plain indexed column, no DB FK — house
    convention; the trip delete route clears dangling links).

    `owner_id` is set by every authenticated write; it is nullable only so the
    pre-auth / auth-off single-user world (user is None) can still write. The
    own-only scope filter (app/scope.py) shows an entry to its author and admins
    only."""
    __tablename__ = "journal_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), index=True)
    entry_date: Mapped[str] = mapped_column(String(10), index=True)  # ISO date, the day it is ABOUT
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    trip_id: Mapped[str | None] = mapped_column(String(36), index=True)  # soft link; attach later
    body: Mapped[str] = mapped_column(Text, default="")
    values: Mapped[dict] = mapped_column(JsonCol, default=dict)  # mood, tags[], place label…
    place_lat: Mapped[float | None] = mapped_column(Float)
    place_lon: Mapped[float | None] = mapped_column(Float)
    place_label: Mapped[str | None] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(16), default="api")  # mobile|web|share|api
    duplicated_from: Mapped[str | None] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class SavedPlace(Base):
    """Run·dere 4.7 — a place the person saved from a Google/Apple Maps link
    before deciding which trip, or when, it belongs to (YONDER-200, D-166).

    Deliberately NOT a TravelItem: a place link is a deterministic parse, not an
    extraction, so it never enters the capture→extract→resolve→review queue the
    bookings use (D-167). It waits here until the person files it to a trip,
    which turns it into a (possibly undated) timeline activity and consumes the
    row — so there is no trip link to dangle (house rule 3).

    Own-only, like the journal: `owner_id` is set by every authenticated write
    and only its owner sees the row. `place_key` (provider:id, or provider:name:
    rounded coords) makes re-saving the same place idempotent per owner. The
    server looks nothing up for this row: `address`/`category` are whatever the
    client (iOS MapKit) or the optional Photon step supplied (D-146)."""
    __tablename__ = "saved_places"
    # One row per place per owner, enforced by the database (D-169): two requests
    # saving the same place at once cannot both insert. (A NULL owner — the auth-off
    # single-user world — is not covered by a unique index; the service checks.)
    __table_args__ = (Index("uq_saved_places_owner_key", "owner_id", "place_key",
                            unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), index=True)
    place_key: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200), default="", index=True)  # near-duplicate check
    # saved = waiting to be filed; filed = became a timeline activity; dismissed =
    # the person removed it. The last two are kept as tombstones (D-169) so
    # re-importing a list never re-adds what was already handled.
    status: Mapped[str] = mapped_column(String(10), default="saved", server_default="saved",
                                        index=True)
    name: Mapped[str] = mapped_column(String(300))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    address: Mapped[str | None] = mapped_column(String(500))
    category: Mapped[str | None] = mapped_column(String(80))     # as the source gave it
    activity_type: Mapped[str] = mapped_column(String(24), default="tour")  # mapped, editable at filing
    provider: Mapped[str] = mapped_column(String(12), default="other")  # google | apple | other
    source_url: Mapped[str | None] = mapped_column(String(2000))
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="api")      # ios | web | mcp | api
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class TripExpense(Base):
    """Run·dere 4.8 — an expense or a settle-up payment on a trip (SPEC-10,
    YONDER-172, D-173).

    Follows the trip's sharing (D-174): whoever can view the trip sees it, whoever
    can edit the trip can change it; `owner_id` records who entered it. `payer_id`
    is a TravelerIdentity id on the trip's roster (D-175), so an off-platform guest
    can pay. Amounts are decimal strings in the expense's own `currency`; `rate`
    is units of the trip's expense currency per 1 unit of `currency` (D-176), NULL
    when unknown (then it's left out of the balances). A settlement (D-177) is the
    same row with kind="settlement": the payer paid the one share's traveller.
    Soft links on trip_id and activity_id (house rule 3)."""
    __tablename__ = "trip_expenses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    trip_id: Mapped[str] = mapped_column(String(36), index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(12), default="expense")      # expense | settlement
    title: Mapped[str] = mapped_column(String(200), default="")
    amount: Mapped[str] = mapped_column(String(32))                       # decimal string
    currency: Mapped[str] = mapped_column(String(3))
    rate: Mapped[str | None] = mapped_column(String(32))                  # trip currency per 1 unit
    rate_source: Mapped[str | None] = mapped_column(String(12))           # same | entered | discover
    spent_on: Mapped[str | None] = mapped_column(String(10), index=True)  # ISO date
    payer_id: Mapped[int] = mapped_column(Integer)
    split: Mapped[str] = mapped_column(String(8), default="equal")        # equal | exact
    category: Mapped[str] = mapped_column(String(16), default="other")
    activity_id: Mapped[str | None] = mapped_column(String(36), index=True)
    # D-181: the planned item this actual fulfils (soft link), for plan vs actual.
    budget_item_id: Mapped[str | None] = mapped_column(String(36), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="api")        # ios | web | mcp | api
    # Where the person was when they logged it (YONDER-212). Captured by the iOS
    # app only; web, MCP and agents send none. Same names as JournalEntry.
    place_lat: Mapped[float | None] = mapped_column(Float)
    place_lon: Mapped[float | None] = mapped_column(Float)
    place_label: Mapped[str | None] = mapped_column(String(256))
    place_accuracy_m: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class TripBudgetItem(Base):
    """A PLANNED cost on a trip — the budget (SPEC-10, D-178/D-179). Separate from
    TripExpense on purpose: only actuals feed balances, settle-up and the trip's
    actual cost (D-178). One estimated amount for the travellers tagged on it
    (TripBudgetTraveler, D-184) — never multiplied by people or days; a room
    for two is one cost. Research agents save `draft` rows
    (D-180); the person keeps them (`kept`), and agents never overwrite a kept
    row. Same sharing as expenses (D-174). Soft links (house rule 3)."""
    __tablename__ = "trip_budget_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    trip_id: Mapped[str] = mapped_column(String(36), index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(16), default="other")
    amount: Mapped[str] = mapped_column(String(32))                       # estimate, decimal string
    currency: Mapped[str] = mapped_column(String(3))
    rate: Mapped[str | None] = mapped_column(String(32))
    rate_source: Mapped[str | None] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(8), default="kept")        # draft | kept
    # D-184: when it applies for these travellers, if not the whole trip (no multiplying).
    start_date: Mapped[str | None] = mapped_column(String(10))
    end_date: Mapped[str | None] = mapped_column(String(10))
    place_key: Mapped[str | None] = mapped_column(String(128))            # "JP:Tokyo", optional
    activity_id: Mapped[str | None] = mapped_column(String(36), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    sources: Mapped[list | None] = mapped_column(JsonCol)                 # [{title, url}]
    source: Mapped[str] = mapped_column(String(16), default="api")        # ios | web | mcp | api
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class TripBudgetTraveler(Base):
    """Who a planned cost is for (D-184): it counts only for these travellers, and
    only they (and whoever entered it) can see it (D-183). Soft link, no DB FK."""
    __tablename__ = "trip_budget_travelers"

    budget_item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    traveler_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)


class TripExpenseComment(Base):
    """A comment on an actual expense, so the people on it can talk it through
    (D-185). Visible exactly where the expense is (D-183). Soft links."""
    __tablename__ = "trip_expense_comments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    expense_id: Mapped[str] = mapped_column(String(36), index=True)
    trip_id: Mapped[str] = mapped_column(String(36), index=True)
    author_id: Mapped[str | None] = mapped_column(String(36), index=True)   # users.id
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TripExpenseShare(Base):
    """Who an expense is for, and their part of it in the expense's currency
    (SPEC-10, D-175). Always stored explicitly — an equal split too — so a later
    roster change never rewrites a past expense. Soft link, no DB FK."""
    __tablename__ = "trip_expense_shares"

    expense_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    traveler_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    amount: Mapped[str] = mapped_column(String(32))
