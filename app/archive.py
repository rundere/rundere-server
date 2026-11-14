"""Run·dere — the archive: a person's own data as a zip of JSON (SPEC-13 §2, D-241).

Format 1 (`rundere-archive`, SPEC-13 §2.2):

    manifest.json                 names the format, lists and checksums every file
    README.txt                    the same in plain words
    profile/account.json          the account and its own log
    profile/traveller.json        the person's traveller and homes
    profile/discovery.json        Discovery preferences
    profile/packing.json          packing preferences, inventory, templates, bags
    trips/<trip id>.json          one per trip, owned or shared
    places.json                   saved places, lists, filings
    journal.json                  journal entries
    documents/index.json          every stored file, and the ones retention removed
    documents/<sha256>.<ext>      the files still stored

Rules every writer here keeps (SPEC-13 §2.1, §2.3):

- **Allow-lists.** A writer names each field it emits. No writer serialises a
  model row wholesale, so a new column is not exported until it is added here
  and to the schema in schemas/archive/v1/ (strict, checked by
  tests/test_archive_export.py against a real export).
- **Never wider than the app.** Reads go through app/scope.py (and the cost
  rules of app/expenses.py, D-183), and then narrow to rows that carry this
  person's own id: an admin's view of ownerless rows, and support access
  (D-211), are never part of anyone's archive.
- **Never a credential (D-151).** Nothing is read from the tables
  app/ownership.py marks `never`.
- **Other people are names.** Each trip file has its own `travellers` list of
  `{ref, name, is_me}`; nothing else about another person leaves the server.
- **Generated paths.** A file's name inside the zip is its sha256 and detected
  type, never the name it was uploaded with.

`build()` writes the zip; app/archive_jobs.py runs it in the background.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app import scope
from app.config import settings
from app.models import (AccountEvent, Bag, DiscoveryExclusion, DiscoveryFinding,
                        DiscoveryItem, DiscoveryProfile, Document, HomeProfile,
                        InventoryItem, JournalEntry, PackingItem, PackingPlan,
                        PackingProfile, PackingTemplate, PackingTemplateItem, PlaceList,
                        PlaceListMember, SavedPlace, SavedPlaceFiling, TravelItem,
                        TravelerIdentity, Trip, TripActivity, TripBudgetItem,
                        TripExpenseComment, TripMember, TripRate, TripTraveler, User)

FORMAT = "rundere-archive"
FORMAT_VERSION = 1

# What an archive never holds (SPEC-13 §2.6), in plain words, for the manifest
# and the README, so a reader isn't left guessing.
NOT_INCLUDED = (
    "Passwords, sign-in tokens, API keys and tokens, connected apps, and the details "
    "of your AirTrail and AdventureLog connections.",
    "Your email-forward address.",
    "Raw captures (forwarded emails, saved pages) and captures still waiting in "
    "review. What a logged capture produced is in the trip files.",
    "The AI call log and Discovery run history.",
    "Server-wide data: the packing catalogue, the phrasebook, rules and settings.",
    "Who your trips are shared with.",
    "Other people's data. On a trip shared with you, only what you added; other "
    "travellers appear as names.",
    "Anything kept in AirTrail or AdventureLog. Those services have their own exports.",
)

_EXT = {"application/pdf": "pdf", "text/calendar": "ics", "message/rfc822": "eml"}
_SIGN_IN = {"local": "local", "oidc": "sso", "proxy": "proxy"}


# --------------------------------------------------------------------------- JSON conventions (§2.2)
def _z(dt: datetime | None) -> str | None:
    """An instant as UTC ending in Z (D-224). SQLite hands back naive UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _s(value) -> str | None:
    """Optional text: a missing value is null, never an empty string."""
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def _place(row) -> dict | None:
    """Where something was written or logged, when the device said."""
    if row.place_lat is None and row.place_lon is None and not row.place_label:
        return None
    return {"lat": row.place_lat, "lon": row.place_lon, "label": _s(row.place_label),
            "accuracy_m": row.place_accuracy_m}


def _dump(doc: dict) -> bytes:
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- profile/
def account_section(session: Session, user: User) -> dict:
    """The account and its own log. Never the password hash, the SSO subject,
    `token_version` or anything else that signs someone in (D-151)."""
    events = (session.query(AccountEvent).filter(AccountEvent.user_id == user.id)
              .order_by(AccountEvent.at, AccountEvent.id).all())

    def by(actor_id) -> str:
        if actor_id is None:
            return "the server"
        return "you" if actor_id == user.id else "an admin"

    return {
        "username": user.username,
        "email": _s(user.email),
        "sign_in": _SIGN_IN.get(user.auth_source, "local"),
        "created_at": _z(user.created_at),
        "is_admin": bool(user.is_admin),
        # Support access (D-211): whether admins may view, and until when.
        "support_access": {"on": bool(user.share_with_admins),
                           "until": _z(user.support_until)},
        # Action and time only: `detail` can name other rows, so it stays out.
        "account_log": [{"action": e.action, "at": _z(e.at), "by": by(e.actor_id)}
                        for e in events],
    }


def traveller_section(session: Session, user: User) -> dict:
    tid = user.traveler_id
    me = session.get(TravelerIdentity, tid) if tid is not None else None
    homes = []
    if me is not None:
        # traveler_id NULL = the server's defaults, never a person's (§1.3).
        homes = (session.query(HomeProfile).filter(HomeProfile.traveler_id == me.id)
                 .order_by(HomeProfile.effective_from, HomeProfile.id).all())
    return {
        "traveller": None if me is None else {
            "name": me.display_name,
            "aliases": [str(a) for a in (me.aliases or [])],
            "nationality": _s(me.nationality),
            "base_currency": _s(me.base_currency),
            "timezone": _s(me.timezone),
            "notes": _s(me.notes),
        },
        "homes": [{
            "kind": h.kind, "label": h.label, "iata": _s(h.iata),
            "lat": h.lat, "lon": h.lon, "radius_km": h.radius_km,
            "country": _s(h.country), "effective_from": _s(h.effective_from),
            "effective_to": _s(h.effective_to), "notes": _s(h.notes),
        } for h in homes],
    }


def _profile(session: Session, model, user: User) -> dict | None:
    if user.traveler_id is None:
        return None
    row = (session.query(model).filter(model.traveler_id == user.traveler_id)
           .order_by(model.updated_at.desc()).first())
    if row is None:
        return None
    return {"values": dict(row.values or {}), "created_at": _z(row.created_at),
            "updated_at": _z(row.updated_at)}


def discovery_section(session: Session, user: User) -> dict:
    return {"profile": _profile(session, DiscoveryProfile, user)}


def _own(session: Session, model, user: User):
    """Rows of a `user_id` own-only table (inventory, templates, bags): the
    app's own-only rule, then this person's rows alone."""
    return session.query(model).filter(scope.own_filter(model, user),
                                       model.user_id == user.id)


def packing_section(session: Session, user: User) -> dict:
    inventory = _own(session, InventoryItem, user).order_by(
        InventoryItem.category, InventoryItem.label, InventoryItem.id).all()
    templates = _own(session, PackingTemplate, user).order_by(
        PackingTemplate.name, PackingTemplate.id).all()
    bags = _own(session, Bag, user).order_by(Bag.label, Bag.id).all()
    lines: dict[str, list] = {}
    if templates:
        for line in (session.query(PackingTemplateItem)
                     .filter(PackingTemplateItem.template_id.in_([t.id for t in templates]))
                     .order_by(PackingTemplateItem.id).all()):
            lines.setdefault(line.template_id, []).append(line)
    return {
        "profile": _profile(session, PackingProfile, user),
        "inventory": [{
            "id": i.id, "type_key": _s(i.type_key), "label": i.label,
            "category": i.category, "tags": [str(t) for t in (i.tags or [])],
            "quantity": i.quantity, "must_have": bool(i.must_have),
            "volume_l": i.volume_l, "brand": _s(i.brand), "notes": _s(i.notes),
            "enabled": bool(i.enabled), "kind": _s(i.kind), "usage_rate": i.usage_rate,
            "reorder_threshold": i.reorder_threshold, "reorder_url": _s(i.reorder_url),
            "created_at": _z(i.created_at), "updated_at": _z(i.updated_at),
        } for i in inventory],
        "templates": [{
            "id": t.id, "type_key": t.type_key, "name": t.name, "keyword": _s(t.keyword),
            "created_at": _z(t.created_at), "updated_at": _z(t.updated_at),
            "items": [{"inventory_item_id": line.inventory_item_id,
                       "quantity": line.quantity} for line in lines.get(t.id, [])],
        } for t in templates],
        "bags": [{
            "id": b.id, "label": b.label, "kind": b.kind, "capacity": _s(b.capacity),
            "enabled": bool(b.enabled), "created_at": _z(b.created_at),
            "updated_at": _z(b.updated_at),
        } for b in bags],
    }


# --------------------------------------------------------------------------- trips/
class _People:
    """A trip file's `travellers` list (SPEC-13 §2.2): each traveller id used
    in the file becomes a ref local to that file, with a name and `is_me`.
    The server-wide traveller id never leaves."""

    def __init__(self, session: Session, user: User):
        self._session, self._me = session, user.traveler_id
        self._refs: dict[int, str] = {}

    def ref(self, traveler_id) -> str | None:
        if traveler_id is None:
            return None
        if traveler_id not in self._refs:
            self._refs[traveler_id] = f"t{len(self._refs) + 1}"
        return self._refs[traveler_id]

    def refs(self, traveler_ids) -> list[str]:
        return [self.ref(t) for t in sorted(traveler_ids)]

    def listing(self) -> list[dict]:
        names = {}
        if self._refs:
            names = dict(self._session.query(TravelerIdentity.id, TravelerIdentity.display_name)
                         .filter(TravelerIdentity.id.in_(list(self._refs))).all())
        return [{"ref": ref, "name": names.get(tid) or "Former traveller",
                 "is_me": tid == self._me}
                for tid, ref in self._refs.items()]


def _activity(a: TripActivity, people: _People, who: dict) -> dict:
    return {
        "id": a.id, "type": a.type, "starts_at": _s(a.starts_at),
        "values": dict(a.values or {}), "geo": a.geo,
        # No rows = everyone on the trip (app/trips/who.py).
        "for": people.refs(who.get(a.id, [])),
        "from_capture": a.source_item_key is not None,
        "created_at": _z(a.created_at), "updated_at": _z(a.updated_at),
    }


def _expense(e, shares, people: _People, user: User) -> dict:
    return {
        "id": e.id, "kind": e.kind, "title": _s(e.title),
        "amount": e.amount, "currency": e.currency,
        "rate": _s(e.rate), "rate_source": _s(e.rate_source), "rate_date": _s(e.rate_date),
        "spent_on": _s(e.spent_on), "payer": people.ref(e.payer_id),
        "split": e.split, "category": e.category,
        "activity_id": _s(e.activity_id), "budget_item_id": _s(e.budget_item_id),
        "notes": _s(e.notes), "source": e.source, "place": _place(e),
        "entered_by_me": e.owner_id == user.id,
        "shares": [{"traveller": people.ref(s.traveler_id), "amount": s.amount}
                   for s in shares],
        "created_at": _z(e.created_at), "updated_at": _z(e.updated_at),
    }


def _planned(b: TripBudgetItem, for_ids, people: _People, user: User) -> dict:
    return {
        "id": b.id, "title": _s(b.title), "category": b.category,
        "amount": b.amount, "currency": b.currency,
        "rate": _s(b.rate), "rate_source": _s(b.rate_source), "rate_date": _s(b.rate_date),
        "status": b.status, "start_date": _s(b.start_date), "end_date": _s(b.end_date),
        "place_key": _s(b.place_key), "activity_id": _s(b.activity_id),
        "notes": _s(b.notes),
        "sources": [{"title": _s((s or {}).get("title")), "url": _s((s or {}).get("url"))}
                    for s in (b.sources or []) if isinstance(s, dict)],
        "source": b.source, "for": people.refs(for_ids),
        "entered_by_me": b.owner_id == user.id,
        "created_at": _z(b.created_at), "updated_at": _z(b.updated_at),
    }


def _discover(session: Session, trip_id: str, user: User) -> dict:
    """This person's own Discover for the trip (per person since 4.1): keyed
    by `user_id`, never anyone else's on the same trip."""
    findings = (session.query(DiscoveryFinding)
                .filter(DiscoveryFinding.trip_id == trip_id,
                        DiscoveryFinding.user_id == user.id)
                .order_by(DiscoveryFinding.place_key, DiscoveryFinding.topic,
                          DiscoveryFinding.id).all())
    items: dict[str, list] = {}
    for it in (session.query(DiscoveryItem)
               .filter(DiscoveryItem.trip_id == trip_id, DiscoveryItem.user_id == user.id)
               .order_by(DiscoveryItem.created_at, DiscoveryItem.id).all()):
        items.setdefault(it.finding_id, []).append(it)
    excluded = (session.query(DiscoveryExclusion.place_key)
                .filter(DiscoveryExclusion.trip_id == trip_id,
                        DiscoveryExclusion.user_id == user.id)
                .order_by(DiscoveryExclusion.place_key).all())
    return {
        "findings": [{
            "id": f.id, "topic": f.topic, "scope": f.scope, "place_key": f.place_key,
            "status": f.status, "body_md": _s(f.body_md), "data": f.data,
            "sources": f.sources, "confidence": _s(f.confidence),
            "resolver": _s(f.resolver), "steer_note": _s(f.steer_note),
            "generated_at": _z(f.generated_at),
            "items": [{
                "id": it.id, "topic": it.topic, "place_key": it.place_key,
                "kind": it.kind, "name": it.name,
                "neighbourhood": _s(it.neighbourhood), "address": _s(it.address),
                "lat": it.lat, "lon": it.lon, "website": _s(it.website),
                "what_to_get": _s(it.what_to_get), "price_tier": it.price_tier,
                "starts_on": _s(it.starts_on), "ends_on": _s(it.ends_on),
                "when_text": _s(it.when_text), "venue": _s(it.venue),
                "details": _s(it.details), "duration": _s(it.duration),
                "booking": it.booking, "sources": it.sources, "extra": it.extra,
                "origin": it.origin, "agent": _s(it.agent),
                "hidden": bool(it.hidden), "hidden_reason": _s(it.hidden_reason),
                "edited": bool(it.edited), "activity_id": _s(it.activity_id),
                "created_at": _z(it.created_at), "updated_at": _z(it.updated_at),
            } for it in items.get(f.id, [])],
        } for f in findings],
        "excluded_places": [row[0] for row in excluded],
    }


def _packing(session: Session, trip_id: str, user: User) -> dict | None:
    """This person's own packing for the trip (per person, D-123)."""
    plan = (session.query(PackingPlan)
            .filter(PackingPlan.trip_id == trip_id, PackingPlan.user_id == user.id)
            .order_by(PackingPlan.generated_at.desc()).first())
    lines = (session.query(PackingItem)
             .filter(PackingItem.trip_id == trip_id, PackingItem.user_id == user.id)
             .order_by(PackingItem.category, PackingItem.item_key, PackingItem.id).all())
    if plan is None and not lines:
        return None
    return {
        "plan": None if plan is None else {
            "status": plan.status, "archetypes": plan.archetypes,
            "manual_archetypes": plan.manual_archetypes, "bag_ids": plan.bag_ids,
            "context": plan.context, "warnings": plan.warnings, "tasks": plan.tasks,
            "generated_at": _z(plan.generated_at), "finished_at": _z(plan.finished_at),
        },
        "items": [{
            "id": p.id, "item_key": p.item_key, "label": p.label, "category": p.category,
            "quantity": p.quantity, "bag": _s(p.bag), "reason": _s(p.reason),
            "source": _s(p.source), "swap_for": _s(p.swap_for), "status": p.status,
            "edited": bool(p.edited), "inventory_item_id": _s(p.inventory_item_id),
            "created_at": _z(p.created_at), "updated_at": _z(p.updated_at),
        } for p in lines],
    }


def _my_comments(session: Session, user: User, expense_ids) -> list[dict]:
    """The person's own comments, on expenses they can see (D-183, D-185)."""
    ids = list(expense_ids)
    if not ids:
        return []
    rows = (session.query(TripExpenseComment)
            .filter(TripExpenseComment.expense_id.in_(ids),
                    TripExpenseComment.author_id == user.id)
            .order_by(TripExpenseComment.created_at, TripExpenseComment.id).all())
    return [{"id": c.id, "expense_id": c.expense_id, "body": c.body,
             "created_at": _z(c.created_at)} for c in rows]


def _own_capture_keys(session: Session, user: User, trip_id: str) -> set[str]:
    """Identity keys of this person's own captures filed to the trip. On a
    shared trip a timeline item is theirs only when it was logged from one of
    these: `trip_activities` has no author column (SPEC-13 §2.4)."""
    rows = (session.query(TravelItem.canonical_key)
            .filter(scope.capture_filter(user), TravelItem.owner_id == user.id,
                    TravelItem.trip_id == trip_id,
                    TravelItem.canonical_key.isnot(None)).all())
    return {row[0] for row in rows}


def trip_section(session: Session, user: User, trip: Trip) -> dict:
    """One trip file. A trip the person owns is written in full, as far as
    they can see it themselves; a trip shared with them carries a header and
    only what they added (SPEC-13 §2.4)."""
    from app import expenses as costs
    from app.trips import who as who_svc

    owned = trip.owner_id == user.id
    role = "owner" if owned else scope.trip_role(session, user, trip)
    people = _People(session, user)

    acts_q = session.query(TripActivity).filter(TripActivity.trip_id == trip.id)
    if owned:
        # The roster first, so the owner's file lists everyone on the trip.
        for (tid,) in (session.query(TripTraveler.traveler_id)
                       .filter(TripTraveler.trip_id == trip.id)
                       .order_by(TripTraveler.traveler_id).all()):
            people.ref(tid)
        activities = acts_q.order_by(TripActivity.starts_at, TripActivity.id).all()
    else:
        keys = _own_capture_keys(session, user, trip.id)
        activities = (acts_q.filter(TripActivity.source_item_key.in_(keys))
                      .order_by(TripActivity.starts_at, TripActivity.id).all()) if keys else []
    who = who_svc.activity_map(session, [a.id for a in activities])

    # Costs: only the people on a cost see it (D-183), the trip's owner included.
    seen, share_map = costs.visible_expenses(session, trip, user)
    expenses = seen if owned else [e for e in seen if e.owner_id == user.id]
    budget = [b for b in costs.budget_of(session, trip.id)
              if (costs.can_see_budget(session, user, b) if owned else b.owner_id == user.id)]
    budget_for = costs.budget_people(session, [b.id for b in budget])
    rates = []
    if owned:
        rates = (session.query(TripRate).filter(TripRate.trip_id == trip.id)
                 .order_by(TripRate.rate_date, TripRate.currency, TripRate.id).all())

    doc = {
        "id": trip.id,
        "shared_with_me": not owned,
        "my_role": role,
        "name": trip.name,
        "start_date": _s(trip.start_date),
        "end_date": _s(trip.end_date),
        # A shared trip's file is a header: the rest of the trip isn't theirs.
        "notes": _s(trip.notes) if owned else None,
        "expense_currency": _s(trip.expense_currency) if owned else None,
        "source": trip.source if owned else None,
        "created_at": _z(trip.created_at) if owned else None,
        "updated_at": _z(trip.updated_at) if owned else None,
        "timeline": [_activity(a, people, who) for a in activities],
        "expenses": [_expense(e, share_map.get(e.id, []), people, user) for e in expenses],
        "planned_costs": [_planned(b, budget_for.get(b.id, []), people, user)
                          for b in budget],
        # Never who fetched a rate (SPEC-13 §1.5).
        "rates": [{"currency": r.currency, "quote": r.quote, "rate_date": r.rate_date,
                   "rate": r.rate, "as_of": _s(r.as_of), "source": r.source,
                   "fetched_at": _z(r.fetched_at)} for r in rates],
        "my_comments": _my_comments(session, user, [e.id for e in seen]),
        "discover": _discover(session, trip.id, user),
        "packing": _packing(session, trip.id, user),
    }
    # Last, so it lists exactly the people the file refers to.
    doc["travellers"] = people.listing()
    return doc


def trips_for(session: Session, user: User) -> list[Trip]:
    """The trips in a person's archive: the ones they own, and the ones shared
    with them as a traveller or by an explicit share. Every one passes the
    app's own trip gate; a trip seen only as an admin (ownerless, or through
    support access, D-211) is in nobody's archive."""
    mine = {row[0] for row in
            session.query(TripMember.trip_id).filter(TripMember.user_id == user.id).all()}
    if user.traveler_id is not None:
        mine |= {row[0] for row in session.query(TripTraveler.trip_id)
                 .filter(TripTraveler.traveler_id == user.traveler_id).all()}
    visible = (session.query(Trip).filter(scope.trip_filter(session, user))
               .order_by(Trip.start_date, Trip.id).all())
    return [t for t in visible
            if t.owner_id == user.id or (t.owner_id is not None and t.id in mine)]


# --------------------------------------------------------------------------- places.json
def places_section(session: Session, user: User) -> dict:
    from app import places as places_svc
    # Removed places are tombstones (D-169): exported and marked, so a later
    # import doesn't bring them back.
    rows = (session.query(SavedPlace)
            .filter(places_svc.visible(user), SavedPlace.owner_id == user.id)
            .order_by(SavedPlace.created_at, SavedPlace.id).all())
    filings: dict[str, list] = {}
    for f in (session.query(SavedPlaceFiling).filter(SavedPlaceFiling.owner_id == user.id)
              .order_by(SavedPlaceFiling.created_at, SavedPlaceFiling.id).all()):
        filings.setdefault(f.place_id, []).append(f)
    lists = (session.query(PlaceList).filter(PlaceList.owner_id == user.id)
             .order_by(PlaceList.position, PlaceList.created_at, PlaceList.id).all())
    members: dict[str, list] = {}
    for m in (session.query(PlaceListMember).filter(PlaceListMember.owner_id == user.id)
              .order_by(PlaceListMember.position, PlaceListMember.added_at,
                        PlaceListMember.id).all()):
        members.setdefault(m.list_id, []).append(m)
    return {
        "places": [{
            "id": p.id, "place_key": p.place_key, "status": p.status,
            "dismissed": p.status == places_svc.DISMISSED,
            "name": p.name, "lat": p.lat, "lon": p.lon, "address": _s(p.address),
            "category": _s(p.category), "activity_type": p.activity_type,
            "provider": p.provider, "source_url": _s(p.source_url), "notes": _s(p.notes),
            "source": p.source, "todo": _s(p.todo), "priority": p.priority,
            "visited_on": _s(p.visited_on), "locality": _s(p.locality),
            "region": _s(p.region), "country_code": _s(p.country_code),
            "locality_source": _s(p.locality_source),
            "filed_to": [{"trip_id": f.trip_id, "activity_id": _s(f.activity_id),
                          "filed_at": _z(f.created_at)} for f in filings.get(p.id, [])],
            "created_at": _z(p.created_at), "updated_at": _z(p.updated_at),
        } for p in rows],
        "lists": [{
            "id": lst.id, "name": lst.name, "emoji": _s(lst.emoji),
            "description": _s(lst.description), "position": lst.position,
            "source": _s(lst.source),
            "places": [{"place_id": m.place_id, "position": m.position,
                        "added_at": _z(m.added_at)} for m in members.get(lst.id, [])],
            "created_at": _z(lst.created_at), "updated_at": _z(lst.updated_at),
        } for lst in lists],
    }


# --------------------------------------------------------------------------- journal.json
def journal_section(session: Session, user: User) -> dict:
    """Every entry of theirs, wherever it's attached; never anyone else's
    (D-134). The own-only filter, then this person's rows alone (an admin's
    view of ownerless entries isn't theirs)."""
    rows = (session.query(JournalEntry)
            .filter(scope.journal_filter(user), JournalEntry.owner_id == user.id)
            .order_by(JournalEntry.entry_date, JournalEntry.captured_at,
                      JournalEntry.id).all())
    return {"entries": [{
        "id": e.id, "entry_date": e.entry_date, "captured_at": _z(e.captured_at),
        "captured_tz": _s(e.captured_tz), "trip_id": _s(e.trip_id), "body": e.body or "",
        "values": dict(e.values or {}), "place": _place(e), "source": e.source,
        "duplicated_from": _s(e.duplicated_from),
        "created_at": _z(e.created_at), "updated_at": _z(e.updated_at),
    } for e in rows]}


# --------------------------------------------------------------------------- documents/
def _doc_path(doc: Document) -> str:
    """Where a file sits inside the zip: its sha256 and detected type. The
    uploaded name is only ever a field in the index, so a crafted filename
    can't write outside the folder the zip is opened into (SPEC-13 §2.2)."""
    return f"documents/{doc.sha256}.{_EXT.get(doc.content_type, 'bin')}"


def documents_section(session: Session, user: User) -> tuple[dict, list[tuple[str, Path]]]:
    """(the index, the stored files as (path in the zip, path on disk)). Only
    its owner ever sees a file (D-196)."""
    from app import documents as store
    rows = (session.query(Document).filter(Document.owner_id == user.id)
            .order_by(Document.created_at, Document.id).all())
    index, files = [], []
    for d in rows:
        on_disk = store.path_for(d) if d.purged_at is None else None
        stored = on_disk is not None and on_disk.is_file()
        remove_on = None
        if d.purged_at is None:
            remove_on = store.removal_date(session, d).isoformat()
        index.append({
            "id": d.id, "filename": _s(d.filename), "content_type": d.content_type,
            "size_bytes": d.size_bytes, "sha256": d.sha256, "trip_id": _s(d.trip_id),
            "source": d.source, "uploaded_at": _z(d.created_at),
            "remove_on": remove_on, "removed_at": _z(d.purged_at),
            "path": _doc_path(d) if stored else None,
        })
        if stored:
            files.append((_doc_path(d), on_disk))
    return {"documents": index}, files


# --------------------------------------------------------------------------- README.txt
def readme(user: User, exported_at: str) -> str:
    lines = [
        "Run·dere archive",
        "================",
        "",
        f"Account: {user.username}",
        f"Exported: {exported_at} (UTC)",
        f"Format: {FORMAT}, version {FORMAT_VERSION}",
        "",
        "This is a copy of your own data from a Run·dere server, as JSON files.",
        "",
        "What is in it",
        "-------------",
        "manifest.json           The format, and every file here with its checksum.",
        "profile/account.json    Your account and its log.",
        "profile/traveller.json  Your traveller profile and your homes.",
        "profile/discovery.json  Your Discovery preferences.",
        "profile/packing.json    Your packing preferences, inventory, templates and bags.",
        "trips/<id>.json         One file per trip. A trip you own is there in full.",
        "                        A trip shared with you (\"shared_with_me\": true) holds",
        "                        only what you added to it.",
        "places.json             Your saved places and lists.",
        "journal.json            Your journal.",
        "documents/index.json    Every file you uploaded, including ones already removed.",
        "documents/<sha256>.*    The files still stored, named by their checksum.",
        "",
        "Times are UTC and end in Z. Dates are YYYY-MM-DD. Money is a decimal string",
        "next to its currency. Other people appear only as names, in each trip file's",
        "\"travellers\" list.",
        "",
        "What is not in it",
        "-----------------",
    ]
    lines += [f"- {item}" for item in NOT_INCLUDED]
    lines += [
        "",
        "Keep this file somewhere safe: it is your whole record in one place, and it",
        "has no password.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- the zip
def archive_name(user: User, on: date | None = None) -> str:
    """`rundere-archive-<username>-<YYYYMMDD>.zip`, with the username reduced
    to characters that are safe in a Content-Disposition header."""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in user.username)
    day = (on or datetime.now(timezone.utc).date()).strftime("%Y%m%d")
    return f"rundere-archive-{safe.strip('.') or 'account'}-{day}.zip"


def sections(session: Session, user: User) -> tuple[list[tuple[str, str, dict, int]],
                                                    list[tuple[str, Path]]]:
    """Every JSON file of the archive as (path, section, document, count), and
    the stored files to copy in."""
    packing = packing_section(session, user)
    places = places_section(session, user)
    journal = journal_section(session, user)
    index, files = documents_section(session, user)
    out = [
        ("profile/account.json", "account", account_section(session, user), 1),
        ("profile/traveller.json", "traveller", traveller_section(session, user), 1),
        ("profile/discovery.json", "discovery", discovery_section(session, user), 1),
        ("profile/packing.json", "packing", packing, len(packing["inventory"])),
    ]
    for trip in trips_for(session, user):
        doc = trip_section(session, user, trip)
        # A trip id is a server-made uuid; the path never comes from user text.
        out.append((f"trips/{trip.id}.json", "trip", doc, len(doc["timeline"])))
    out += [
        ("places.json", "places", places, len(places["places"])),
        ("journal.json", "journal", journal, len(journal["entries"])),
        ("documents/index.json", "documents", index, len(index["documents"])),
    ]
    return out, files


def build(session: Session, user: User, dest: Path) -> dict:
    """Write `user`'s archive to `dest` (a new file). Returns
    `{"sha256", "bytes", "files"}` for the zip. The caller owns the folder,
    the file's permissions and its lifetime (app/archive_jobs.py)."""
    exported_at = _z(datetime.now(timezone.utc))
    docs, files = sections(session, user)
    entries: list[dict] = []
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        def add(path: str, section: str, data: bytes, count: int | None) -> None:
            zf.writestr(path, data)
            entries.append({"path": path, "section": section,
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "bytes": len(data), "count": count})

        add("README.txt", "readme", readme(user, exported_at).encode("utf-8"), None)
        for path, section, doc, count in docs:
            add(path, section, _dump(doc), count)
        for path, on_disk in files:
            # Streamed: a person's files can add up to their whole quota.
            digest, size = hashlib.sha256(), 0
            with open(on_disk, "rb") as src, zf.open(path, "w") as out:
                while chunk := src.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            entries.append({"path": path, "section": "document",
                            "sha256": digest.hexdigest(), "bytes": size, "count": None})
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "server_version": settings.app_version,
            "exported_at": exported_at,
            "username": user.username,
            "files": entries,
            "not_included": list(NOT_INCLUDED),
        }
        zf.writestr("manifest.json", _dump(manifest))
    digest = hashlib.sha256()
    with open(dest, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "bytes": dest.stat().st_size,
            "files": len(entries) + 1}
