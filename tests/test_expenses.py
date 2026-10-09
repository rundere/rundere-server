"""SPEC-10 (YONDER-172) — trip expenses, balances and settle-up over /api/v1.

Equal splits divide to the minor unit with the leftover to the first travellers
(D-175); exact splits must add up; payers and sharers are on the roster; each
expense keeps its own currency and the rate used, filled from Discover when blank,
unconverted when unknown (D-176); settlements net out (D-177); expenses follow the
trip's sharing (D-174); trip and activity deletes clean up (rule 3)."""
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
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, DiscoveryFinding, TravelerIdentity, Trip, TripActivity,
                            TripExpense, TripExpenseShare, TripMember, TripTraveler, User)
    from app.trips.purge import purge_trip

    Base.metadata.create_all(engine)
    client = TestClient(app)

    @pytest.fixture(autouse=True)
    def _clean():
        """Leave nothing behind: other suites (resolving) read every trip and
        traveller in the shared test database."""
        yield
        with SessionLocal() as s:
            for t in s.query(Trip).filter(Trip.name.like("Exp %")).all():
                s.query(DiscoveryFinding).filter_by(trip_id=t.id).delete()
                purge_trip(s, t)
            # By name: a test that purged its own trip took the roster rows with it.
            s.query(TravelerIdentity).filter(TravelerIdentity.display_name.like("Exp %")).delete(
                synchronize_session=False)
            s.query(User).filter(User.username.like("ex-%")).delete(synchronize_session=False)
            s.commit()

    def _user(name):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                u = User(username=name, auth_source="local")
                s.add(u)
                s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _h(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _trip(owner, names=("Ana", "Ben", "Cy")):
        """A trip owned by `owner` with a roster; returns (trip id, {name: traveller id})."""
        tag = uuid.uuid4().hex[:6]
        with SessionLocal() as s:
            t = Trip(name=f"Exp {tag}", source="manual", owner_id=owner.id,
                     start_date="2031-05-01", end_date="2031-05-09")
            s.add(t)
            s.flush()
            ids = {}
            for n in names:
                ti = TravelerIdentity(display_name=f"Exp {n} {tag}", kind="guest")
                s.add(ti)
                s.flush()
                s.add(TripTraveler(trip_id=t.id, traveler_id=ti.id))
                ids[n] = ti.id
            s.commit()
            return t.id, ids

    def _url(tid, rest=""):
        return f"/api/v1/trips/{tid}/expenses{rest}"

    def _net(overview):
        return {b["traveler_id"]: b["net"] for b in overview["balances"]}

    def test_equal_split_divides_to_the_cent_and_balances():
        owner = _user("ex-owner")
        tid, p = _trip(owner)
        r = client.post(_url(tid), headers=_h(owner), json={
            "amount": "100", "currency": "eur", "payer_id": p["Ana"], "title": "Dinner",
            "category": "food", "spent_on": "2031-05-02"})
        assert r.status_code == 201, r.text
        e = r.json()
        assert e["currency"] == "EUR" and e["rate"] == "1" and e["rate_source"] == "same"
        assert [s["amount"] for s in e["shares"]] == ["33.34", "33.33", "33.33"]
        ov = client.get(_url(tid), headers=_h(owner)).json()
        assert ov["currency"] == "EUR" and ov["total"] == "100"
        assert _net(ov) == {p["Ana"]: "66.66", p["Ben"]: "-33.33", p["Cy"]: "-33.33"}
        assert {(t["from_id"], t["to_id"], t["amount"]) for t in ov["transfers"]} == {
            (p["Ben"], p["Ana"], "33.33"), (p["Cy"], p["Ana"], "33.33")}

    def test_equal_split_between_some_and_exact_split():
        owner = _user("ex-owner")
        tid, p = _trip(owner)
        r = client.post(_url(tid), headers=_h(owner), json={
            "amount": "30", "currency": "EUR", "payer_id": p["Ben"],
            "traveler_ids": [p["Ben"], p["Cy"]]})
        assert [s["traveler_id"] for s in r.json()["shares"]] == sorted([p["Ben"], p["Cy"]])
        bad = client.post(_url(tid), headers=_h(owner), json={
            "amount": "50", "currency": "EUR", "payer_id": p["Ana"], "split": "exact",
            "shares": {str(p["Ana"]): "20", str(p["Ben"]): "20"}})
        assert bad.status_code == 400 and "add up to 40" in bad.json()["detail"]
        ok = client.post(_url(tid), headers=_h(owner), json={
            "amount": "50", "currency": "EUR", "payer_id": p["Ana"], "split": "exact",
            "shares": {str(p["Ana"]): "20", str(p["Ben"]): "30", str(p["Cy"]): "0"}})
        assert ok.status_code == 201
        assert {s["traveler_id"]: s["amount"] for s in ok.json()["shares"]} == {
            p["Ana"]: "20", p["Ben"]: "30"}
        net = _net(client.get(_url(tid), headers=_h(owner)).json())
        # Ben paid 30 for Ben+Cy (+15) and owes Ana 30; Ana paid 50, her share 20.
        assert net == {p["Ana"]: "30", p["Ben"]: "-15", p["Cy"]: "-15"}

    def test_roster_and_input_checks():
        owner = _user("ex-owner")
        tid, p = _trip(owner)
        other_tid, q = _trip(owner, names=("Zed",))
        base = {"amount": "10", "currency": "EUR", "payer_id": p["Ana"]}
        cases = [({**base, "payer_id": q["Zed"]}, "isn't on this trip"),
                 ({**base, "traveler_ids": [q["Zed"]]}, "isn't on this trip"),
                 ({**base, "amount": "-5"}, "more than 0"),
                 ({**base, "amount": "lots"}, "not an amount"),
                 ({**base, "currency": "EURO"}, "currency code"),
                 ({**base, "category": "bribes"}, "category"),
                 ({**base, "spent_on": "May 2"}, "not a date")]
        for body, msg in cases:
            r = client.post(_url(tid), headers=_h(owner), json=body)
            assert r.status_code == 400 and msg in r.json()["detail"], (body, r.text)
        with SessionLocal() as s:
            a = TripActivity(trip_id=other_tid, type="dining", starts_at="2031-05-02T19:00",
                             values={"name": "Elsewhere"})
            s.add(a)
            s.commit()
            aid = a.id
        r = client.post(_url(tid), headers=_h(owner), json={**base, "activity_id": aid})
        assert r.status_code == 400 and "isn't on this trip" in r.json()["detail"]
        empty, _ = _trip(owner, names=())
        r = client.post(_url(empty), headers=_h(owner), json={**base})
        assert r.status_code == 400 and "travellers first" in r.json()["detail"]

    def test_currencies_rates_and_unconverted():
        owner = _user("ex-owner")
        tid, p = _trip(owner, names=("Ana", "Ben"))
        client.post(_url(tid), headers=_h(owner), json={
            "amount": "20", "currency": "EUR", "payer_id": p["Ana"]})       # sets EUR
        entered = client.post(_url(tid), headers=_h(owner), json={
            "amount": "1000", "currency": "JPY", "payer_id": p["Ben"], "rate": "0.006"}).json()
        assert entered["rate_source"] == "entered" and entered["amount_in_trip_currency"] == "6"
        assert [s["amount"] for s in entered["shares"]] == ["500", "500"]   # yen: no cents
        unknown = client.post(_url(tid), headers=_h(owner), json={
            "amount": "50", "currency": "CHF", "payer_id": p["Ben"]}).json()
        assert unknown["rate"] is None and unknown["amount_in_trip_currency"] is None
        ov = client.get(_url(tid), headers=_h(owner)).json()
        assert ov["unconverted"] == [unknown["id"]] and ov["total"] == "26"
        # Discover's stored rates (per CAD) fill a blank rate: 1 CAD = 0.68 EUR = 0.65 CHF.
        with SessionLocal() as s:
            s.add(DiscoveryFinding(trip_id=tid, user_id=owner.id, topic="currency",
                                   scope="country", place_key="CH",
                                   data={"rates": {"EUR": {"per_CAD": 0.68},
                                                   "CHF": {"per_CAD": 0.65}}}))
            s.commit()
        filled = client.patch(_url(tid, f"/{unknown['id']}"), headers=_h(owner),
                              json={"currency": "CHF"}).json()
        assert filled["rate_source"] == "discover" and filled["amount_in_trip_currency"] == "52.31"
        # Switching the trip to CHF: EUR comes from Discover, the entered yen rate is cleared.
        ov = client.put(_url(tid, "/currency"), headers=_h(owner), json={"currency": "chf"}).json()
        assert ov["currency"] == "CHF" and ov["unconverted"] == [entered["id"]]
        by_id = {e["id"]: e for e in ov["expenses"]}
        assert by_id[filled["id"]]["rate_source"] == "same"
        assert by_id[ov["expenses"][-1]["id"]]["rate_source"] == "discover"

    def test_settlement_nets_out_and_edits_follow():
        owner = _user("ex-owner")
        tid, p = _trip(owner, names=("Ana", "Ben"))
        e = client.post(_url(tid), headers=_h(owner), json={
            "amount": "80", "currency": "EUR", "payer_id": p["Ana"]}).json()
        same = client.post(f"/api/v1/trips/{tid}/settlements", headers=_h(owner),
                           json={"from_id": p["Ana"], "to_id": p["Ana"], "amount": "1"})
        assert same.status_code == 400
        st = client.post(f"/api/v1/trips/{tid}/settlements", headers=_h(owner),
                         json={"from_id": p["Ben"], "to_id": p["Ana"], "amount": "40"})
        assert st.status_code == 201 and st.json()["kind"] == "settlement"
        ov = client.get(_url(tid), headers=_h(owner)).json()
        assert _net(ov) == {p["Ana"]: "0", p["Ben"]: "0"} and ov["transfers"] == []
        assert ov["total"] == "80"                         # settlements aren't spend
        # Raising the expense keeps who it's split between; the settlement follows its amount.
        up = client.patch(_url(tid, f"/{e['id']}"), headers=_h(owner), json={"amount": "100"})
        assert [s["amount"] for s in up.json()["shares"]] == ["50", "50"]
        st2 = client.patch(_url(tid, f"/{st.json()['id']}"), headers=_h(owner),
                           json={"amount": "50"}).json()
        assert [s["amount"] for s in st2["shares"]] == ["50"]
        assert _net(client.get(_url(tid), headers=_h(owner)).json()) == {
            p["Ana"]: "0", p["Ben"]: "0"}
        assert client.delete(_url(tid, f"/{e['id']}"), headers=_h(owner)).status_code == 204
        with SessionLocal() as s:
            assert s.query(TripExpenseShare).filter_by(expense_id=e["id"]).count() == 0

    def test_expenses_follow_trip_sharing():
        """D-174 on top of D-122: a traveller on the roster edits, an explicit
        (view-only) share sees but can't change, a stranger gets nothing."""
        owner, viewer, stranger = _user("ex-owner"), _user("ex-viewer"), _user("ex-stranger")
        tid, p = _trip(owner)
        traveller = _user(f"ex-trav-{tid[:6]}")
        with SessionLocal() as s:
            s.add(TripMember(trip_id=tid, user_id=viewer.id))
            s.get(User, traveller.id).traveler_id = p["Cy"]
            s.commit()
        traveller = _user(f"ex-trav-{tid[:6]}")
        body = {"amount": "12", "currency": "EUR", "payer_id": p["Cy"]}
        r = client.post(_url(tid), headers=_h(traveller), json=body)
        assert r.status_code == 201 and r.json()["owner_id"] == traveller.id
        eid = r.json()["id"]
        assert len(client.get(_url(tid), headers=_h(owner)).json()["expenses"]) == 1
        assert len(client.get(_url(tid), headers=_h(viewer)).json()["expenses"]) == 1
        assert client.post(_url(tid), headers=_h(viewer), json=body).status_code == 403
        assert client.delete(_url(tid, f"/{eid}"), headers=_h(viewer)).status_code == 403
        for call in (lambda: client.get(_url(tid), headers=_h(stranger)),
                     lambda: client.post(_url(tid), headers=_h(stranger), json=body),
                     lambda: client.patch(_url(tid, f"/{eid}"), headers=_h(stranger),
                                          json={"title": "mine"}),
                     lambda: client.delete(_url(tid, f"/{eid}"), headers=_h(stranger)),
                     lambda: client.post(f"/api/v1/trips/{tid}/settlements",
                                         headers=_h(stranger),
                                         json={"from_id": p["Ana"], "to_id": p["Cy"],
                                               "amount": "4"})):
            assert call().status_code == 404
        # An expense id from another trip isn't reachable through this one.
        other, q = _trip(stranger, names=("Zed",))
        assert client.patch(_url(other, f"/{eid}"), headers=_h(stranger),
                            json={"title": "x"}).status_code == 404

    def test_activity_delete_unlinks_and_trip_delete_purges():
        owner = _user("ex-owner")
        tid, p = _trip(owner, names=("Ana",))
        with SessionLocal() as s:
            a = TripActivity(trip_id=tid, type="dining", starts_at="2031-05-02T19:00",
                             values={"name": "Noma"})
            s.add(a)
            s.commit()
            aid = a.id
        e = client.post(_url(tid), headers=_h(owner), json={
            "amount": "300", "currency": "DKK", "payer_id": p["Ana"],
            "activity_id": aid}).json()
        assert e["activity_id"] == aid
        assert client.delete(f"/api/v1/trips/{tid}/activities/{aid}",
                             headers=_h(owner)).status_code == 204
        with SessionLocal() as s:
            row = s.get(TripExpense, e["id"])
            assert row is not None and row.activity_id is None
            purge_trip(s, s.get(Trip, tid))
            s.commit()
            assert s.query(TripExpense).filter_by(trip_id=tid).count() == 0
            assert s.query(TripExpenseShare).filter_by(expense_id=e["id"]).count() == 0

    # ------------------------------------------------------------------ planned (D-178–D-181)
    def _budget(tid, rest=""):
        return f"/api/v1/trips/{tid}/budget{rest}"

    def test_planned_costs_expand_and_stay_out_of_balances():
        owner = _user("ex-owner")
        tid, p = _trip(owner, names=("Ana", "Ben"))            # 9 days (05-01 → 05-09)
        hotel = client.post(_budget(tid), headers=_h(owner), json={
            "title": "Hotel", "category": "lodging", "amount": "100", "currency": "EUR",
            "basis": "day"}).json()
        assert hotel["status"] == "kept" and hotel["times"] == 9 and hotel["total"] == "900"
        lunch = client.post(_budget(tid), headers=_h(owner), json={
            "title": "Lunch", "category": "food", "amount": "15", "currency": "EUR",
            "basis": "person_day"}).json()
        assert lunch["times"] == 18 and lunch["total"] == "270"
        bad = client.post(_budget(tid), headers=_h(owner), json={
            "amount": "5", "currency": "EUR", "basis": "per_week"})
        assert bad.status_code == 400 and "basis" in bad.json()["detail"]
        # An actual logged against the hotel plan.
        e = client.post(_url(tid), headers=_h(owner), json={
            "amount": "880", "currency": "EUR", "payer_id": p["Ana"], "category": "lodging",
            "budget_item_id": hotel["id"]}).json()
        assert e["budget_item_id"] == hotel["id"]
        ov = client.get(_url(tid), headers=_h(owner)).json()
        plan = ov["plan"]
        assert plan["people"] == 2 and plan["days"] == 9
        assert plan["planned"] == "1170" and plan["actual"] == "880" and plan["drafts"] == "0"
        by_cat = {c["category"]: c for c in plan["by_category"]}
        assert by_cat["lodging"] == {"category": "lodging", "planned": "900", "drafts": "0",
                                     "actual": "880"}
        assert {i["id"]: i["actual"] for i in plan["items"]}[hotel["id"]] == "880"
        # D-178: the plan never touches who owes whom.
        assert _net(ov) == {p["Ana"]: "440", p["Ben"]: "-440"} and ov["total"] == "880"
        # Removing the planned item keeps the actual, unlinked.
        assert client.delete(_budget(tid, f"/{hotel['id']}"), headers=_h(owner)).status_code == 204
        with SessionLocal() as s:
            assert s.get(TripExpense, e["id"]).budget_item_id is None

    def test_agent_budget_lands_as_drafts_and_respects_kept():
        owner = _user("ex-owner")
        tid, p = _trip(owner, names=("Ana",))
        mine = client.post(_budget(tid), headers=_h(owner), json={
            "title": "Hotel", "category": "lodging", "amount": "90", "currency": "EUR",
            "basis": "day"}).json()
        research = {"items": [
            {"title": "Hotel", "category": "lodging", "amount": "120", "currency": "EUR",
             "basis": "day"},
            {"title": "Metro pass", "category": "transport", "amount": "30", "currency": "EUR",
             "basis": "person", "sources": [{"title": "RATP", "url": "https://ratp.fr"},
                                            {"title": "bad", "url": "javascript:x"}]},
            {"title": "Broken", "category": "food", "amount": "-1", "currency": "EUR"}]}
        out = client.post(_budget(tid, "/batch"), headers=_h(owner), json=research).json()
        assert out["counts"] == {"drafted": 1, "error": 1, "kept_already": 1}
        items = {i["title"]: i for i in out["plan"]["items"]}
        assert items["Hotel"]["id"] == mine["id"] and items["Hotel"]["amount"] == "90"
        assert items["Metro pass"]["status"] == "draft"
        assert items["Metro pass"]["sources"] == [{"title": "RATP", "url": "https://ratp.fr"}]
        assert out["plan"]["drafts"] == "30" and out["plan"]["planned"] == "810"
        # A re-run replaces the agent's drafts in those categories, nothing else.
        again = client.post(_budget(tid, "/batch"), headers=_h(owner), json={"items": [
            {"title": "Metro pass", "category": "transport", "amount": "28",
             "currency": "EUR", "basis": "person"}]}).json()
        metro = [i for i in again["plan"]["items"] if i["title"] == "Metro pass"]
        assert len(metro) == 1 and metro[0]["amount"] == "28"
        # The person keeps it; it's theirs from then on.
        kept = client.patch(_budget(tid, f"/{metro[0]['id']}"), headers=_h(owner),
                            json={"status": "kept"}).json()
        assert kept["status"] == "kept"
        third = client.post(_budget(tid, "/batch"), headers=_h(owner), json={"items": [
            {"title": "Metro pass", "category": "transport", "amount": "1", "currency": "EUR"}]})
        assert third.json()["counts"] == {"kept_already": 1}

    def test_budget_follows_trip_sharing_and_cleanup():
        owner, viewer, stranger = _user("ex-owner"), _user("ex-viewer"), _user("ex-stranger")
        tid, p = _trip(owner, names=("Ana",))
        with SessionLocal() as s:
            s.add(TripMember(trip_id=tid, user_id=viewer.id))
            s.commit()
        b = client.post(_budget(tid), headers=_h(owner), json={
            "title": "Tickets", "category": "activities", "amount": "40",
            "currency": "EUR", "basis": "person"}).json()
        assert client.get(_url(tid), headers=_h(viewer)).json()["plan"]["items"][0]["id"] == b["id"]
        body = {"title": "x", "amount": "1", "currency": "EUR"}
        assert client.post(_budget(tid), headers=_h(viewer), json=body).status_code == 403
        assert client.post(_budget(tid, "/batch"), headers=_h(viewer),
                           json={"items": [body]}).status_code == 403
        assert client.patch(_budget(tid, f"/{b['id']}"), headers=_h(stranger),
                            json={"title": "mine"}).status_code == 404
        assert client.delete(_budget(tid, f"/{b['id']}"), headers=_h(stranger)).status_code == 404
        other, _ = _trip(stranger, names=("Zed",))
        assert client.post(_url(other), headers=_h(stranger), json={
            "amount": "1", "currency": "EUR", "payer_id": _["Zed"],
            "budget_item_id": b["id"]}).status_code == 400
        with SessionLocal() as s:
            from app.models import TripBudgetItem
            purge_trip(s, s.get(Trip, tid))
            s.commit()
            assert s.query(TripBudgetItem).filter_by(trip_id=tid).count() == 0
