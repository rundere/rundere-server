"""Launch fixes: YONDER-340 (a flight's travellers join the trip it files under),
YONDER-341 (a stay booked with dates but no times still shows them in the
activity editor) and YONDER-319 (a person renames their own traveller)."""
import random
import string
import sys
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
    import app.committing as C
    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, TravelerIdentity, TravelItem, Trip, TripActivity,
                            TripTraveler, User)
    from app.resolving import load_identities
    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    sys.path.insert(0, str(ROOT / "tests"))
    from test_committing import FakeAL, FakeAT, _add, _flight_payload

    def _tag():
        # Letters only: the passenger matcher compares name words.
        return "".join(random.choices(string.ascii_lowercase, k=6))

    @pytest.fixture()
    def made(monkeypatch):
        """Creates people and cleans up everything they own afterwards."""
        monkeypatch.setattr(settings, "auth_required", True)
        state = {"users": [], "travellers": [], "items": []}

        def person(name, *, generic=False, account=True):
            with SessionLocal() as s:
                t = TravelerIdentity(display_name=name, kind="guest", generic=generic)
                s.add(t)
                s.flush()
                state["travellers"].append(t.id)
                u = None
                if account:
                    u = User(username=f"lf-{_tag()}", auth_source="local", traveler_id=t.id)
                    s.add(u)
                    s.flush()
                    state["users"].append(u.id)
                s.commit()
                if u is not None:
                    s.refresh(u)
                    s.expunge(u)
                return u, t.id
        state["person"] = person
        yield state
        with SessionLocal() as s:
            trips = s.query(Trip).filter(Trip.owner_id.in_(state["users"])).all()
            for tr in trips:
                s.query(TripActivity).filter_by(trip_id=tr.id).delete()
                s.query(TripTraveler).filter_by(trip_id=tr.id).delete()
                s.delete(tr)
            s.query(TravelItem).filter(TravelItem.id.in_(state["items"])).delete(
                synchronize_session=False)
            s.query(User).filter(User.id.in_(state["users"])).delete(synchronize_session=False)
            s.query(TravelerIdentity).filter(
                TravelerIdentity.id.in_(state["travellers"])).delete(synchronize_session=False)
            s.commit()

    def _ck(u):
        return {settings.auth_cookie_name: authn.issue_access_token(u)}

    def _commit_flight(made, owner, names, group, *, minter):
        with SessionLocal() as s:
            payload = _flight_payload()
            payload["travelers"] = [{"raw_name": n} for n in names]
            it = _add(s, payload, targets=["yonder.activity"])
            it.canonical_key = f"flight:pnr:{_tag()}"
            it.owner_id = owner.id
            it.resolution = {**it.resolution, "mint_group": group,
                             "is_minter": minter, "mint_yonder_trip": True}
            s.commit()
            made["items"].append(it.id)
            C.commit_item(s, it, FakeAL(), FakeAT(), identities=load_identities(s),
                          dry_run=False)
            s.commit()
            return it.trip_id

    def _roster(trip_id):
        with SessionLocal() as s:
            return sorted(r.traveler_id for r in
                          s.query(TripTraveler).filter_by(trip_id=trip_id).all())

    # ---- YONDER-340 ---------------------------------------------------------

    def test_minted_trip_has_its_capturer_and_passengers(made):
        tag = _tag()
        ada, ada_t = made["person"](f"Ada Fictus {tag}")
        _, bo_t = made["person"](f"Bo Inventa {tag}", account=False)
        _, guest_t = made["person"](f"Guest Inventa {tag}", generic=True, account=False)
        trip_id = _commit_flight(made, ada, [f"Bo Inventa {tag}", f"Guest Inventa {tag}"],
                                 f"lf-{tag}", minter=True)
        assert trip_id
        roster = _roster(trip_id)
        assert ada_t in roster and bo_t in roster       # capturer + named passenger
        assert guest_t not in roster                    # a placeholder never joins

    def test_second_flight_adds_new_passenger_once(made):
        tag = _tag()
        ada, ada_t = made["person"](f"Ada Fictus {tag}")
        _, bo_t = made["person"](f"Bo Inventa {tag}", account=False)
        _, cy_t = made["person"](f"Cy Fictus {tag}", account=False)
        trip_id = _commit_flight(made, ada, [f"Bo Inventa {tag}"], f"lf-{tag}", minter=True)
        again = _commit_flight(made, ada, [f"Bo Inventa {tag}", f"Cy Fictus {tag}"],
                               f"lf-{tag}", minter=False)
        assert again == trip_id
        assert _roster(trip_id) == sorted([ada_t, bo_t, cy_t])     # each exactly once

    # ---- YONDER-341 ---------------------------------------------------------

    def _activity(owner, atype, values):
        with SessionLocal() as s:
            t = Trip(name="Lf trip", source="manual", owner_id=owner.id)
            s.add(t)
            s.flush()
            a = TripActivity(trip_id=t.id, type=atype, values=values,
                             starts_at=values.get("when") or "")
            s.add(a)
            s.commit()
            return f"/trips/{t.id}/activity/{a.id}/edit"

    def test_stay_with_dates_only_gets_usual_times(made):
        ada, _ = made["person"](f"Ada Fictus {_tag()}")
        url = _activity(ada, "lodging", {"name": "Hotel Inventado",
                                         "when": "2031-05-02", "endWhen": "2031-05-04"})
        r = client.get(url, cookies=_ck(ada))
        assert r.status_code == 200
        assert 'value="2031-05-02T15:00"' in r.text and 'value="2031-05-04T11:00"' in r.text
        assert "The booking gave no time" in r.text

    def test_stay_with_times_is_left_alone(made):
        ada, _ = made["person"](f"Ada Fictus {_tag()}")
        url = _activity(ada, "lodging", {"name": "Hotel Inventado",
                                         "when": "2031-05-02T16:00", "endWhen": "2031-05-04T10:00"})
        r = client.get(url, cookies=_ck(ada))
        assert 'value="2031-05-02T16:00"' in r.text and 'value="2031-05-04T10:00"' in r.text
        assert "The booking gave no time" not in r.text

    def test_other_dated_activity_shows_a_date_field(made):
        ada, _ = made["person"](f"Ada Fictus {_tag()}")
        url = _activity(ada, "event", {"name": "Fictional Fair", "when": "2031-05-02"})
        r = client.get(url, cookies=_ck(ada))
        assert 'type="date"' in r.text and 'value="2031-05-02"' in r.text

    def test_editor_is_the_trips_own(made):
        ada, _ = made["person"](f"Ada Fictus {_tag()}")
        bo, _ = made["person"](f"Bo Inventa {_tag()}")
        url = _activity(ada, "lodging", {"name": "Hotel Inventado", "when": "2031-05-02"})
        assert client.get(url, cookies=_ck(bo)).status_code == 404

    # ---- YONDER-319 ---------------------------------------------------------

    def _name(tid):
        with SessionLocal() as s:
            return s.get(TravelerIdentity, tid).display_name

    def test_rename_own_traveller(made):
        tag = _tag()
        ada, ada_t = made["person"](f"Ada Fictus {tag}")
        r = client.post("/trips/settings/travel-profile", cookies=_ck(ada),
                        data={"display_name": f"Ada Q Fictus {tag}"})
        assert r.status_code == 200 and _name(ada_t) == f"Ada Q Fictus {tag}"

    def test_rename_refuses_a_taken_name(made):
        tag = _tag()
        ada, ada_t = made["person"](f"Ada Fictus {tag}")
        _, bo_t = made["person"](f"Bo Inventa {tag}")
        r = client.post("/trips/settings/travel-profile", cookies=_ck(ada),
                        data={"display_name": f"bo inventa {tag}"})
        assert "already a traveller called" in r.text
        assert _name(ada_t) == f"Ada Fictus {tag}" and _name(bo_t) == f"Bo Inventa {tag}"

    def test_blank_name_keeps_the_name(made):
        tag = _tag()
        ada, ada_t = made["person"](f"Ada Fictus {tag}")
        client.post("/trips/settings/travel-profile", cookies=_ck(ada),
                    data={"display_name": "  "})
        assert _name(ada_t) == f"Ada Fictus {tag}"
