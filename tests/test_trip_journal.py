"""YONDER-215 — journal entries linked to a trip show on the trip page.

Own-only (SPEC-6 D-134): each viewer sees just their own entries on the trip;
sharing a trip — as a co-traveller, a view-only member or an admin — never
shows someone else's journal."""
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, JournalEntry, TravelerIdentity, Trip, TripMember,
                            TripTraveler, User)
    from app.trips.purge import purge_trip

    Base.metadata.create_all(engine)
    client = TestClient(app)

    @pytest.fixture(autouse=True)
    def _auth(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        yield
        with SessionLocal() as s:
            for t in s.query(Trip).filter(Trip.name.like("Tj %")).all():
                s.query(JournalEntry).filter(JournalEntry.trip_id == t.id).delete()
                purge_trip(s, t)
            s.query(TravelerIdentity).filter(
                TravelerIdentity.display_name.like("Tj %")).delete(synchronize_session=False)
            s.query(User).filter(User.username.like("tj-%")).delete(synchronize_session=False)
            s.commit()

    def _setup():
        """Ana owns the trip, Ben travels on it, Vi views it, Ad is an admin."""
        tag = uuid.uuid4().hex[:6]
        with SessionLocal() as s:
            people, users = {}, {}
            for n in ("Ana", "Ben"):
                ti = TravelerIdentity(display_name=f"Tj {n} {tag}", kind="guest")
                s.add(ti)
                s.flush()
                people[n] = ti.id
            for n in ("Ana", "Ben", "Vi", "Ad"):
                u = User(username=f"tj-{n.lower()}-{tag}", auth_source="local",
                         traveler_id=people.get(n), is_admin=(n == "Ad"))
                s.add(u)
                s.flush()
                users[n] = u
            t = Trip(name=f"Tj {tag}", source="manual", owner_id=users["Ana"].id,
                     start_date="2031-05-01", end_date="2031-05-09")
            s.add(t)
            s.flush()
            for tid in people.values():
                s.add(TripTraveler(trip_id=t.id, traveler_id=tid))
            s.add(TripMember(trip_id=t.id, user_id=users["Vi"].id))
            for n, text in (("Ana", "Ana harbour tacos"), ("Ben", "Ben private note"),
                            ("Ad", "Admin own note")):
                s.add(JournalEntry(owner_id=users[n].id, trip_id=t.id, entry_date="2031-05-03",
                                   body=text, source="web"))
            s.add(JournalEntry(owner_id=users["Ana"].id, trip_id=None,
                               entry_date="2031-05-04", body="Ana unlinked note"))
            s.commit()
            ck = {n: {settings.auth_cookie_name: authn.issue_access_token(u)}
                  for n, u in users.items()}
            return t.id, ck

    def test_your_linked_entries_show_on_the_trip():
        tid, ck = _setup()
        html = client.get(f"/trips/{tid}", cookies=ck["Ana"]).text
        assert 'data-tab="journal"' in html
        assert "harbour tacos" in html and "unlinked note" not in html
        assert "/journal#je-" in html

    def test_nobody_sees_anyone_elses_journal_on_a_shared_trip():
        tid, ck = _setup()
        ana = client.get(f"/trips/{tid}", cookies=ck["Ana"]).text
        assert "Ben private note" not in ana and "Admin own note" not in ana
        ben = client.get(f"/trips/{tid}", cookies=ck["Ben"]).text
        assert "Ben private note" in ben and "harbour tacos" not in ben
        vi = client.get(f"/trips/{tid}", cookies=ck["Vi"]).text
        # Vi only views the trip: no Journal tab at all (D-199), and nobody's entries.
        assert "harbour tacos" not in vi and 'data-tab="journal"' not in vi
        ad = client.get(f"/trips/{tid}", cookies=ck["Ad"]).text   # admin, Ana shares
        assert "harbour tacos" not in ad and "Ben private note" not in ad
