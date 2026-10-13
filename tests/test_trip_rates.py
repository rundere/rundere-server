"""YONDER-237 / SPEC-10 D-195 — one dated exchange-rate table per TRIP.

Everyone on a trip converting a cost from the same day gets the same rate
(fetched once from Frankfurter, stubbed here); edits that don't change the
currency, day or rate never re-rate an old expense; an ended trip fetches
nothing (D-157); with no feed, the nearest rate on the table, then the trip
owner's Discover snapshot, fill in; the refresh endpoint fills only the
caller's visible unconverted costs (D-183) and needs edit access."""
import sys
import uuid
from datetime import date
from decimal import Decimal
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
    from app import authn, rates
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, DiscoveryFinding, TravelerIdentity, Trip, TripMember,
                            TripRate, TripTraveler, User)
    from app.trips.purge import purge_trip

    Base.metadata.create_all(engine)
    client = TestClient(app)
    TODAY = date.today().isoformat()

    @pytest.fixture(autouse=True)
    def _clean():
        yield
        with SessionLocal() as s:
            for t in s.query(Trip).filter(Trip.name.like("Rate %")).all():
                s.query(DiscoveryFinding).filter_by(trip_id=t.id).delete()
                purge_trip(s, t)
            s.query(TravelerIdentity).filter(
                TravelerIdentity.display_name.like("Rate %")).delete(synchronize_session=False)
            s.query(User).filter(User.username.like("rt-%")).delete(synchronize_session=False)
            s.commit()

    @pytest.fixture
    def feed(monkeypatch):
        """A stub Frankfurter: {(from, to, day): (rate, as_of)}; records each call."""
        table, calls = {}, []

        def fetch(frm, to, day):
            calls.append((frm, to, day))
            hit = table.get((frm, to, day))
            return (Decimal(hit[0]), hit[1]) if hit else (None, None)
        monkeypatch.setattr(rates, "fetch", fetch)
        return table, calls

    def _user(name, traveler_id=None):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                u = User(username=name, auth_source="local")
                s.add(u)
            u.traveler_id = traveler_id
            s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _h(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _trip(owner, start="2026-01-01", end="2031-12-31", names=("Ana", "Ben")):
        tag = uuid.uuid4().hex[:6]
        with SessionLocal() as s:
            t = Trip(name=f"Rate {tag}", source="manual", owner_id=owner.id,
                     start_date=start, end_date=end, expense_currency="EUR")
            s.add(t)
            s.flush()
            ids = {}
            for n in names:
                ti = TravelerIdentity(display_name=f"Rate {n} {tag}", kind="guest")
                s.add(ti)
                s.flush()
                s.add(TripTraveler(trip_id=t.id, traveler_id=ti.id))
                ids[n] = ti.id
            s.commit()
            return t.id, ids

    def _url(tid, rest=""):
        return f"/api/v1/trips/{tid}/expenses{rest}"

    def _add(tid, who, payer, **kw):
        body = {"amount": "100", "currency": "USD", "payer_id": payer,
                "spent_on": "2026-03-02", **kw}
        r = client.post(_url(tid), headers=_h(who), json=body)
        assert r.status_code == 201, r.text
        return r.json()

    def test_one_rate_per_trip_per_day_for_everyone(feed):
        table, calls = feed
        table[("USD", "EUR", "2026-03-02")] = ("0.9", "2026-02-27")   # a weekend → Friday
        owner = _user("rt-owner")
        tid, p = _trip(owner)
        ana, ben = _user("rt-ana", p["Ana"]), _user("rt-ben", p["Ben"])
        a = _add(tid, ana, p["Ana"])
        b = _add(tid, ben, p["Ben"])
        for e in (a, b):
            assert e["rate"] == "0.9" and e["rate_source"] == "frankfurter"
            assert e["rate_date"] == "2026-02-27" and e["amount_in_trip_currency"] == "90"
        assert calls == [("USD", "EUR", "2026-03-02")]        # fetched once, then the table
        # A different day is its own row.
        table[("USD", "EUR", "2026-03-05")] = ("0.95", "2026-03-05")
        c = _add(tid, ana, p["Ana"], spent_on="2026-03-05")
        assert c["rate"] == "0.95" and len(calls) == 2
        with SessionLocal() as s:
            assert s.query(TripRate).filter_by(trip_id=tid).count() == 2

    def test_edits_never_re_rate_an_old_expense(feed):
        table, calls = feed
        table[("USD", "EUR", "2026-03-02")] = ("0.9", "2026-03-02")
        owner = _user("rt-owner2")
        tid, p = _trip(owner)
        ana = _user("rt-ana2", p["Ana"])
        e = _add(tid, ana, p["Ana"])
        # The table's row changes later (someone refreshed it)…
        with SessionLocal() as s:
            s.query(TripRate).filter_by(trip_id=tid).update({"rate": "0.5"})
            s.commit()
        # …and the app re-sends the same currency with a notes edit: no re-rate.
        r = client.patch(_url(tid, f"/{e['id']}"), headers=_h(ana),
                         json={"notes": "tip incl.", "currency": "USD"}).json()
        assert r["rate"] == "0.9" and r["rate_date"] == "2026-03-02"
        # Moving it to another day DOES re-rate, from that day's rate.
        table[("USD", "EUR", "2026-03-04")] = ("0.92", "2026-03-04")
        r = client.patch(_url(tid, f"/{e['id']}"), headers=_h(ana),
                         json={"spent_on": "2026-03-04"}).json()
        assert r["rate"] == "0.92" and r["rate_date"] == "2026-03-04"
        # Setting the balance currency to what it already is changes nothing.
        ov = client.put(_url(tid, "/currency"), headers=_h(ana), json={"currency": "eur"}).json()
        assert {x["id"]: x["rate"] for x in ov["expenses"]}[e["id"]] == "0.92"

    def test_future_days_use_todays_rate(feed):
        table, calls = feed
        table[("USD", "EUR", TODAY)] = ("0.88", TODAY)
        owner = _user("rt-owner3")
        tid, p = _trip(owner)
        e = _add(tid, owner, p["Ana"], spent_on="2031-06-01")
        assert e["rate"] == "0.88" and calls == [("USD", "EUR", TODAY)]

    def test_no_feed_falls_back_to_the_table_then_the_owners_discover(feed):
        owner = _user("rt-owner4")
        tid, p = _trip(owner)
        ana = _user("rt-ana4", p["Ana"])
        # Nothing anywhere → unconverted, never guessed.
        e = _add(tid, ana, p["Ana"])
        assert e["rate"] is None and e["amount_in_trip_currency"] is None
        # The trip OWNER's Discover snapshot fills it — for every traveller alike.
        with SessionLocal() as s:
            s.add(DiscoveryFinding(trip_id=tid, user_id=owner.id, topic="currency",
                                   scope="country", place_key="US",
                                   data={"date": "2026-02-20",
                                         "rates": {"USD": {"per_CAD": 0.7},
                                                   "EUR": {"per_CAD": 0.63}}}))
            s.commit()
        f = _add(tid, ana, p["Ana"], spent_on="2026-03-09")
        assert f["rate"] == "0.9" and f["rate_source"] == "discover"
        assert f["rate_date"] == "2026-02-20"
        # Now it's on the table: the nearest day serves the next one without Discover.
        with SessionLocal() as s:
            s.query(DiscoveryFinding).filter_by(trip_id=tid).delete()
            s.commit()
        g = _add(tid, ana, p["Ana"], spent_on="2026-04-01")
        assert g["rate"] == "0.9"

    def test_ended_trip_fetches_todays_rate_never_backdates(feed):
        """Owner, 2026-10-13: logging a cost after the trip, in a currency the trip
        has no rate for, takes TODAY's rate (the day it's added); once there is
        one, later costs use the table — no backdated fetches."""
        table, calls = feed
        table[("USD", "EUR", TODAY)] = ("0.88", TODAY)
        table[("USD", "EUR", "2020-01-05")] = ("0.7", "2020-01-05")   # never asked for
        owner = _user("rt-owner5")
        tid, p = _trip(owner, start="2020-01-01", end="2020-01-10")
        e = _add(tid, owner, p["Ana"], spent_on="2020-01-05")
        assert e["rate"] == "0.88" and e["rate_date"] == TODAY
        assert calls == [("USD", "EUR", TODAY)]
        e2 = _add(tid, owner, p["Ana"], spent_on="2020-01-07")    # the table now has USD
        assert e2["rate"] == "0.88" and len(calls) == 1
        # A refresh on an ended trip is always today's, whatever date is asked.
        r = client.post(f"/api/v1/trips/{tid}/rates/USD/refresh?date=2020-01-05",
                        headers=_h(owner))
        assert r.status_code == 200 and r.json()["rate"]["rate_date"] == TODAY
        assert calls[-1] == ("USD", "EUR", TODAY)
        # An entered rate still works.
        e3 = _add(tid, owner, p["Ana"], spent_on="2020-01-05", rate="0.91")
        assert e3["rate"] == "0.91" and e3["rate_source"] == "entered"
        assert e3["rate_date"] == "2020-01-05"

    def test_calculator_uses_the_trips_latest_rate_and_offers_today(feed, monkeypatch):
        """D-195: the calculator converts with the trip table's LATEST rate per
        currency, no live call; a rate older than today offers "Update rates for
        today" to editors only, which fetches today's and redraws."""
        from app.config import settings
        table, calls = feed
        owner, viewer = _user("rt-owner8"), _user("rt-viewer8")
        tid, p = _trip(owner)
        with SessionLocal() as s:
            s.add(TripMember(trip_id=tid, user_id=viewer.id))
            for uid in (owner.id, viewer.id):
                s.add(DiscoveryFinding(trip_id=tid, user_id=uid, topic="currency",
                                       scope="country", place_key="US",
                                       data={"currency": "USD", "date": "2026-02-01",
                                             "rates": {"USD": {"per_EUR": 1.25}}}))
            s.add_all([TripRate(trip_id=tid, currency="USD", quote="EUR",
                                rate_date="2026-03-01", rate="0.8", source="frankfurter"),
                       TripRate(trip_id=tid, currency="USD", quote="EUR",
                                rate_date="2026-03-05", rate="0.9", as_of="2026-03-05",
                                source="frankfurter")])
            s.commit()
            fids = {f.user_id: f.id for f in s.query(DiscoveryFinding).filter_by(trip_id=tid)}
        monkeypatch.setattr(settings, "auth_required", True)
        page = client.get(f"/trips/{tid}/finding/{fids[owner.id]}/calculator",
                          headers=_h(owner)).text
        assert "USD 2026-03-05" in page and calls == []          # stored rates only
        assert '"USD": 1.1111' in page                           # 1 EUR = 1/0.9 USD
        assert "Update rates for today" in page
        vpage = client.get(f"/trips/{tid}/finding/{fids[viewer.id]}/calculator",
                           headers=_h(viewer)).text
        assert "USD 2026-03-05" in vpage and "Update rates for today" not in vpage
        assert client.post(f"/trips/{tid}/finding/{fids[viewer.id]}/calculator/update-rates",
                           headers=_h(viewer)).status_code == 403
        table[("USD", "EUR", TODAY)] = ("0.95", TODAY)
        page = client.post(f"/trips/{tid}/finding/{fids[owner.id]}/calculator/update-rates",
                           headers=_h(owner)).text
        assert f"USD {TODAY}" in page and "Update rates for today" not in page
        assert calls == [("USD", "EUR", TODAY)]
        # The app gets the same latest rates in the trip snapshot.
        snap = client.get(f"/api/v1/trips/{tid}", headers=_h(owner)).json()
        assert [(r["currency"], r["rate"], r["rate_date"]) for r in snap["trip_rates"]] == \
            [("USD", "0.95", TODAY)]

    def test_refresh_fills_your_unconverted_costs_only(feed):
        table, calls = feed
        owner = _user("rt-owner6")
        tid, p = _trip(owner)
        ana, ben = _user("rt-ana6", p["Ana"]), _user("rt-ben6", p["Ben"])
        mine = _add(tid, ana, p["Ana"], traveler_ids=[p["Ana"]])          # no feed yet
        hers_only = _add(tid, ben, p["Ben"], traveler_ids=[p["Ben"]])     # Ana can't see it
        assert mine["rate"] is None and hers_only["rate"] is None
        table[("USD", "EUR", "2026-03-02")] = ("0.9", "2026-03-02")
        r = client.post(f"/api/v1/trips/{tid}/rates/usd/refresh?date=2026-03-02",
                        headers=_h(ana))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rate"]["rate"] == "0.9" and body["rate"]["currency"] == "USD"
        assert body["rate"]["quote"] == "EUR" and body["applied"] == [mine["id"]]
        mine_now = next(x for x in client.get(_url(tid), headers=_h(ana)).json()["expenses"]
                        if x["id"] == mine["id"])
        assert mine_now["rate"] == "0.9"
        # Ben's own cost (D-183: invisible to Ana) was not touched by her refresh.
        bens = next(x for x in client.get(_url(tid), headers=_h(ben)).json()["expenses"]
                    if x["id"] == hers_only["id"])
        assert bens["rate"] is None
        # A refresh replaces the table row but never re-rates a cost that has one.
        table[("USD", "EUR", "2026-03-02")] = ("0.5", "2026-03-02")
        r = client.post(f"/api/v1/trips/{tid}/rates/USD/refresh?date=2026-03-02",
                        headers=_h(ana)).json()
        assert r["rate"]["rate"] == "0.5" and r["applied"] == []
        assert next(x for x in client.get(_url(tid), headers=_h(ana)).json()["expenses"]
                    if x["id"] == mine["id"])["rate"] == "0.9"

    def test_refresh_errors_and_scoping(feed):
        owner, viewer, stranger = _user("rt-owner7"), _user("rt-viewer7"), _user("rt-str7")
        tid, p = _trip(owner)
        with SessionLocal() as s:
            s.add(TripMember(trip_id=tid, user_id=viewer.id))     # view-only share
            s.commit()
        url = f"/api/v1/trips/{tid}/rates/USD/refresh"
        assert client.post(url, headers=_h(viewer)).status_code == 403
        assert client.post(url, headers=_h(stranger)).status_code == 404
        r = client.post(url, headers=_h(owner))                   # the feed has nothing
        assert r.status_code == 422 and "no exchange rate" in r.json()["detail"]
        assert client.post(f"/api/v1/trips/{tid}/rates/EUR/refresh",
                           headers=_h(owner)).status_code == 422   # its own currency
        assert client.post(f"/api/v1/trips/{tid}/rates/USDX/refresh",
                           headers=_h(owner)).status_code == 400
        assert client.post(url + "?date=03/02/2026", headers=_h(owner)).status_code == 400

    def test_fetch_parses_the_v2_feed(monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "frankfurter_url", "https://feed.example/v2")
        seen = {}

        def get_json(url, timeout=12, retries=1):
            seen["url"] = url
            return [{"date": "2026-02-27", "base": "USD", "quote": "EUR", "rate": 0.9}]
        monkeypatch.setattr("discover.http.get_json", get_json)
        assert rates.fetch("USD", "EUR", "2026-03-01") == (Decimal("0.9"), "2026-02-27")
        assert "base=USD" in seen["url"] and "quotes=EUR" in seen["url"]
        assert "date=2026-03-01" in seen["url"]
        monkeypatch.setattr(settings, "frankfurter_url", "")
        assert rates.fetch("USD", "EUR", "2026-03-01") == (None, None)
