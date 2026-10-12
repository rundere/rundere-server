"""YONDER-212 — the trip page's Costs tab, and where an expense was logged.

The web Costs tab runs on the same rules as /api/v1 (SPEC-10): the trip gate
(D-174: view to read, edit to write), only the people on a cost see it (D-183),
and the location the iOS app records is shown on the web but never captured or
changed there."""
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
    from app.models import (Base, DiscoveryFinding, TravelerIdentity, Trip, TripExpense,
                            TripMember, TripTraveler, User)
    from app.trips.purge import purge_trip

    Base.metadata.create_all(engine)
    client = TestClient(app)

    @pytest.fixture(autouse=True)
    def _auth(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        yield
        with SessionLocal() as s:
            for t in s.query(Trip).filter(Trip.name.like("Cw %")).all():
                s.query(DiscoveryFinding).filter_by(trip_id=t.id).delete()
                purge_trip(s, t)
            s.query(TravelerIdentity).filter(
                TravelerIdentity.display_name.like("Cw %")).delete(synchronize_session=False)
            s.query(User).filter(User.username.like("cw-%")).delete(synchronize_session=False)
            s.commit()

    def _setup():
        """A trip owned by Ana with Ana, Ben and Cy on the roster; Ana and Ben
        have logins, plus Vi who can only view the trip."""
        tag = uuid.uuid4().hex[:6]
        with SessionLocal() as s:
            people = {}
            for n in ("Ana", "Ben", "Cy"):
                ti = TravelerIdentity(display_name=f"Cw {n} {tag}", kind="guest")
                s.add(ti)
                s.flush()
                people[n] = ti.id
            users = {}
            for n in ("Ana", "Ben", "Vi"):
                u = User(username=f"cw-{n.lower()}-{tag}", auth_source="local",
                         traveler_id=people.get(n))
                s.add(u)
                s.flush()
                users[n] = u
            t = Trip(name=f"Cw {tag}", source="manual", owner_id=users["Ana"].id,
                     start_date="2031-05-01", end_date="2031-05-09")
            s.add(t)
            s.flush()
            for tid in people.values():
                s.add(TripTraveler(trip_id=t.id, traveler_id=tid))
            s.add(TripMember(trip_id=t.id, user_id=users["Vi"].id))
            s.commit()
            cookies = {n: {settings.auth_cookie_name: authn.issue_access_token(u)}
                       for n, u in users.items()}
            headers = {n: {"Authorization": f"Bearer {authn.issue_access_token(u)}"}
                       for n, u in users.items()}
            return t.id, people, cookies, headers

    def _form(people, **over):
        data = {"title": "Dinner", "amount": "30", "currency": "EUR", "spent_on": "2031-05-02",
                "payer_id": str(people["Ana"]), "split": "equal", "category": "food",
                "traveler_ids": [str(people["Ana"]), str(people["Ben"]), str(people["Cy"])]}
        data.update(over)
        return data

    # ------------------------------------------------------------ location (API)

    def test_api_round_trips_the_location_and_keeps_it_on_edit():
        tid, p, _, h = _setup()
        r = client.post(f"/api/v1/trips/{tid}/expenses", headers=h["Ana"], json={
            "amount": "12", "currency": "EUR", "payer_id": p["Ana"], "title": "Coffee",
            "place_lat": 48.8566, "place_lon": 2.3522, "place_label": "Le Marais",
            "place_accuracy_m": 15})
        assert r.status_code == 201, r.text
        e = r.json()
        assert (e["place_lat"], e["place_lon"], e["place_label"], e["place_accuracy_m"]) == \
            (48.8566, 2.3522, "Le Marais", 15)
        # An edit that doesn't mention the location keeps it.
        r = client.patch(f"/api/v1/trips/{tid}/expenses/{e['id']}", headers=h["Ana"],
                         json={"title": "Coffees"})
        assert r.json()["place_label"] == "Le Marais"
        # Nulls clear it as a whole.
        r = client.patch(f"/api/v1/trips/{tid}/expenses/{e['id']}", headers=h["Ana"],
                         json={"place_lat": None, "place_lon": None, "place_label": None,
                               "place_accuracy_m": None})
        assert r.json()["place_lat"] is None and r.json()["place_label"] is None

    def test_api_location_is_optional_and_validated():
        tid, p, _, h = _setup()
        url = f"/api/v1/trips/{tid}/expenses"
        r = client.post(url, headers=h["Ana"], json={"amount": "5", "currency": "EUR",
                                                      "payer_id": p["Ana"]})
        assert r.status_code == 201 and r.json()["place_lat"] is None
        bad = [{"place_lat": 10.0}, {"place_lat": 95.0, "place_lon": 0.0},
               {"place_label": "Somewhere"}, {"place_lat": 1.0, "place_lon": 1.0,
                                              "place_accuracy_m": -3}]
        for extra in bad:
            r = client.post(url, headers=h["Ana"], json={"amount": "5", "currency": "EUR",
                                                          "payer_id": p["Ana"], **extra})
            assert r.status_code == 400, extra

    def test_location_is_only_visible_to_people_on_the_cost():
        """D-183 covers the location too: a traveller who isn't on the expense
        never sees it, nor the expense."""
        tid, p, _, h = _setup()
        r = client.post(f"/api/v1/trips/{tid}/expenses", headers=h["Ana"], json={
            "amount": "8", "currency": "EUR", "payer_id": p["Ana"], "traveler_ids": [p["Ana"]],
            "place_lat": 1.5, "place_lon": 2.5, "place_label": "Secret spot"})
        eid = r.json()["id"]
        ben = client.get(f"/api/v1/trips/{tid}/expenses", headers=h["Ben"]).json()
        assert eid not in [e["id"] for e in ben["expenses"]]
        assert "Secret spot" not in client.get(f"/api/v1/trips/{tid}/expenses",
                                               headers=h["Ben"]).text

    # ------------------------------------------------------------ the web tab

    def test_costs_tab_renders_on_the_trip_page():
        tid, p, ck, _ = _setup()
        html = client.get(f"/trips/{tid}", cookies=ck["Ana"]).text
        assert 'data-tab="costs"' in html and "No expenses yet." in html
        assert f'hx-get="/trips/{tid}/costs/new"' in html

    def test_add_an_expense_from_the_web_and_see_the_balances():
        tid, p, ck, _ = _setup()
        r = client.post(f"/trips/{tid}/costs", cookies=ck["Ana"], data=_form(p))
        assert r.status_code == 200 and r.headers.get("HX-Redirect") == f"/trips/{tid}#costs"
        with SessionLocal() as s:
            e = s.query(TripExpense).filter_by(trip_id=tid).one()
            assert (e.amount, e.source, e.place_lat) == ("30", "web", None)
        html = client.get(f"/trips/{tid}/costs", cookies=ck["Ana"]).text
        assert "Dinner" in html and "30.00 EUR" in html
        assert "is owed 20.00 EUR" in html                 # Ana paid 30 for three
        assert "pays" in html and "Mark paid" in html      # suggested settle-up

    def test_bad_input_rerenders_the_form_with_the_error():
        tid, p, ck, _ = _setup()
        r = client.post(f"/trips/{tid}/costs", cookies=ck["Ana"],
                        data=_form(p, amount="-4"))
        assert r.status_code == 200 and "HX-Redirect" not in r.headers
        assert "bp-error" in r.text and 'value="-4"' in r.text
        with SessionLocal() as s:
            assert s.query(TripExpense).filter_by(trip_id=tid).count() == 0

    def test_exact_split_from_the_web():
        tid, p, ck, _ = _setup()
        data = _form(p, split="exact", amount="10")
        data.update({f"share_{p['Ana']}": "7", f"share_{p['Ben']}": "3"})
        r = client.post(f"/trips/{tid}/costs", cookies=ck["Ana"], data=data)
        assert r.headers.get("HX-Redirect"), r.text
        with SessionLocal() as s:
            e = s.query(TripExpense).filter_by(trip_id=tid).one()
            assert e.split == "exact"

    def test_mark_a_suggested_payment_as_paid():
        tid, p, ck, _ = _setup()
        client.post(f"/trips/{tid}/costs", cookies=ck["Ana"], data=_form(p))
        r = client.post(f"/trips/{tid}/costs/settle", cookies=ck["Ben"],
                        data={"from_id": p["Ben"], "to_id": p["Ana"], "amount": "10"})
        assert r.status_code == 200 and 'id="costs-section"' in r.text
        with SessionLocal() as s:
            assert s.query(TripExpense).filter_by(trip_id=tid, kind="settlement").count() == 1

    def test_web_edit_keeps_the_location_the_app_recorded():
        tid, p, ck, h = _setup()
        eid = client.post(f"/api/v1/trips/{tid}/expenses", headers=h["Ana"], json={
            "amount": "9", "currency": "EUR", "payer_id": p["Ana"], "title": "Taxi",
            "place_lat": 41.39, "place_lon": 2.17, "place_label": "Plaça de Catalunya"}
        ).json()["id"]
        page = client.get(f"/trips/{tid}/costs", cookies=ck["Ana"]).text
        assert "Plaça de Catalunya" in page and "openstreetmap.org/?mlat=41.39000" in page
        modal = client.get(f"/trips/{tid}/costs/{eid}/edit", cookies=ck["Ana"]).text
        assert "Logged at" in modal and "place_lat" not in modal   # shown, not editable
        r = client.post(f"/trips/{tid}/costs/{eid}", cookies=ck["Ana"],
                        data=_form(p, title="Taxi home", amount="9"))
        assert r.headers.get("HX-Redirect"), r.text
        with SessionLocal() as s:
            e = s.get(TripExpense, eid)
            assert (e.title, e.place_label, e.place_lat) == ("Taxi home", "Plaça de Catalunya",
                                                             41.39)

    def test_comment_from_the_web():
        tid, p, ck, h = _setup()
        eid = client.post(f"/api/v1/trips/{tid}/expenses", headers=h["Ana"], json={
            "amount": "9", "currency": "EUR", "payer_id": p["Ana"]}).json()["id"]
        r = client.post(f"/trips/{tid}/costs/{eid}/comment", cookies=ck["Ben"],
                        data={"body": "I'll get the next one"})
        assert "get the next one" in r.text

    def test_delete_from_the_web():
        tid, p, ck, h = _setup()
        eid = client.post(f"/api/v1/trips/{tid}/expenses", headers=h["Ana"], json={
            "amount": "9", "currency": "EUR", "payer_id": p["Ana"]}).json()["id"]
        r = client.post(f"/trips/{tid}/costs/{eid}/delete", cookies=ck["Ana"])
        assert r.headers.get("HX-Redirect")
        with SessionLocal() as s:
            assert s.get(TripExpense, eid) is None

    # ------------------------------------------------------------ scoping

    def test_view_only_share_cannot_write_costs():
        tid, p, ck, h = _setup()
        eid = client.post(f"/api/v1/trips/{tid}/expenses", headers=h["Ana"], json={
            "amount": "9", "currency": "EUR", "payer_id": p["Ana"]}).json()["id"]
        assert client.post(f"/trips/{tid}/costs", cookies=ck["Vi"],
                           data=_form(p)).status_code == 403
        assert client.post(f"/trips/{tid}/costs/settle", cookies=ck["Vi"], data={
            "from_id": p["Ben"], "to_id": p["Ana"], "amount": "1"}).status_code == 403
        assert client.post(f"/trips/{tid}/costs/{eid}/delete",
                           cookies=ck["Vi"]).status_code == 403
        # And the costs they aren't on are invisible to them (D-183).
        assert "9.00 EUR" not in client.get(f"/trips/{tid}/costs", cookies=ck["Vi"]).text
        assert "Add expense" not in client.get(f"/trips/{tid}", cookies=ck["Vi"]).text

    def test_traveller_not_on_a_cost_gets_404s_on_the_web():
        tid, p, ck, h = _setup()
        eid = client.post(f"/api/v1/trips/{tid}/expenses", headers=h["Ana"], json={
            "amount": "9", "currency": "EUR", "payer_id": p["Ana"],
            "traveler_ids": [p["Ana"]]}).json()["id"]
        for call in (lambda: client.get(f"/trips/{tid}/costs/{eid}/edit", cookies=ck["Ben"]),
                     lambda: client.post(f"/trips/{tid}/costs/{eid}", cookies=ck["Ben"],
                                         data=_form(p)),
                     lambda: client.post(f"/trips/{tid}/costs/{eid}/delete", cookies=ck["Ben"]),
                     lambda: client.post(f"/trips/{tid}/costs/{eid}/comment", cookies=ck["Ben"],
                                         data={"body": "hi"})):
            assert call().status_code == 404
        with SessionLocal() as s:
            assert s.get(TripExpense, eid) is not None

    def test_a_stranger_cannot_see_the_costs_tab():
        tid, p, _, _ = _setup()
        with SessionLocal() as s:
            u = User(username=f"cw-stranger-{uuid.uuid4().hex[:6]}", auth_source="local")
            s.add(u)
            s.commit()
            ck = {settings.auth_cookie_name: authn.issue_access_token(u)}
        assert client.get(f"/trips/{tid}/costs", cookies=ck).status_code == 404
        assert client.post(f"/trips/{tid}/costs", cookies=ck, data=_form(p)).status_code == 404

    # ------------------------------------------------------------ currency picker (YONDER-213)

    def _destination(tid, user_id, *codes):
        with SessionLocal() as s:
            for i, c in enumerate(codes):
                s.add(DiscoveryFinding(trip_id=tid, user_id=user_id, topic="currency",
                                       scope="country", place_key=f"P{i}",
                                       data={"currency": c, "rates": {}}))
            s.commit()

    def _ana(tid):
        with SessionLocal() as s:
            return s.query(User).join(Trip, Trip.owner_id == User.id).filter(Trip.id == tid).one()

    def test_currencies_are_suggested_destination_first_then_yours():
        tid, p, ck, h = _setup()
        ana = _ana(tid)
        with SessionLocal() as s:
            s.get(TravelerIdentity, p["Ana"]).base_currency = "CAD"
            s.commit()
        _destination(tid, ana.id, "PTE", "EUR")             # a two-country trip, odd code first
        client.post(f"/api/v1/trips/{tid}/expenses", headers=h["Ana"], json={
            "amount": "5", "currency": "GBP", "payer_id": p["Ana"]})
        ov = client.get(f"/api/v1/trips/{tid}/expenses", headers=h["Ana"]).json()
        # Destination (both), then Ana's own, then the trip's (GBP, set by the first
        # expense) — no duplicates.
        assert ov["suggested_currencies"] == ["PTE", "EUR", "CAD", "GBP"]
        assert ov["default_currency"] == "CAD"              # her own currency
        # Another traveller's Discover findings aren't Ana's suggestions.
        assert "PTE" not in client.get(f"/api/v1/trips/{tid}/expenses",
                                       headers=h["Ben"]).json()["suggested_currencies"]

    def test_the_web_form_is_a_picker_defaulting_to_your_currency():
        tid, p, ck, _ = _setup()
        with SessionLocal() as s:
            s.get(TravelerIdentity, p["Ana"]).base_currency = "CAD"
            s.commit()
        _destination(tid, _ana(tid).id, "EUR")
        modal = client.get(f"/trips/{tid}/costs/new", cookies=ck["Ana"]).text
        assert '<select id="ex-cur"' in modal and 'optgroup label="For this trip"' in modal
        assert modal.index('value="EUR"') < modal.index('value="CAD"')   # destination first
        assert 'value="CAD" selected' in modal                            # yours by default
        assert 'value="JPY"' in modal                                     # and every other code

    def test_me_carries_your_own_currency_and_time_zone():
        tid, p, _, h = _setup()
        with SessionLocal() as s:
            t = s.get(TravelerIdentity, p["Ana"])
            t.base_currency, t.timezone = "CAD", "America/Toronto"
            s.commit()
        me = client.get("/api/v1/auth/me", headers=h["Ana"]).json()
        assert (me["base_currency"], me["timezone"]) == ("CAD", "America/Toronto")
