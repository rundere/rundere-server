"""Food & Drink / Events web views (4.5, YONDER-150/151/152): city tables,
add-to-timeline via the prefilled form, hide/show, summary edits."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, DiscoveryFinding, DiscoveryItem, Trip, TripActivity
    from discover import items
    Base.metadata.create_all(engine)
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    client = TestClient(app)

    def _trip():
        with SessionLocal() as s:
            t = Trip(name="Web items", start_date="2026-10-10", end_date="2026-10-20")
            s.add(t)
            s.flush()
            f = DiscoveryFinding(trip_id=t.id, topic="food", scope="city",
                                 place_key="MA:Harar", status="draft", resolver="agent",
                                 body_md="**Harar**\n\nEat late.",
                                 data={"summary": "Eat late.", "agent": "Claude"})
            e = DiscoveryFinding(trip_id=t.id, topic="events", scope="city",
                                 place_key="MA:Harar", status="draft", resolver="agent",
                                 body_md="x", data={"summary": "Festival season."})
            s.add_all([f, e])
            s.flush()
            items.sync_items(s, f, [items.row_from_input("food", r) for r in (
                {"kind": "restaurant", "name": "Dar Yacout", "what_to_get": "Pastilla",
                 "price_tier": 4, "address": "79 Sidi Ahmed Soussi"},
                {"kind": "bar", "name": "Kabana"})], origin="agent", agent="Claude")
            items.sync_items(s, e, [items.row_from_input("events", {
                "kind": "event", "name": "Film festival", "starts_on": "2026-10-14"})])
            s.commit()
            ids = {i.name: i.id for i in s.query(DiscoveryItem).filter_by(trip_id=t.id)}
            return t.id, f.id, ids

    def _cleanup(tid):
        with SessionLocal() as s:
            for m in (DiscoveryItem, DiscoveryFinding, TripActivity):
                s.query(m).filter(m.trip_id == tid).delete()
            s.query(Trip).filter_by(id=tid).delete()
            s.commit()

    def test_page_shows_summary_tables_and_actions():
        tid, fid, ids = _trip()
        try:
            page = client.get(f"/trips/{tid}").text
            assert "Eat late." in page and "Festival season." in page
            assert "Pastilla" in page and "$$$$" in page and "Restaurants" in page
            assert f"/trips/{tid}/discovery-item/{ids['Dar Yacout']}/to-timeline" in page
            assert "Oct 14" in page and "Events" in page
        finally:
            _cleanup(tid)

    def test_add_to_timeline_form_prefills_and_saving_links_the_item():
        tid, fid, ids = _trip()
        try:
            form = client.get(f"/trips/{tid}/discovery-item/{ids['Dar Yacout']}/to-timeline").text
            assert 'value="dining" selected' in form and "Dar Yacout" in form
            assert "What to get: Pastilla" in form and 'name="discovery_item_id"' in form
            r = client.post(f"/trips/{tid}/activity", data={
                "activity_kind": "dining", "name": "Dar Yacout", "when": "2026-10-16T20:30",
                "discovery_item_id": ids["Dar Yacout"]})
            assert r.headers.get("hx-refresh") == "true"
            with SessionLocal() as s:
                a = s.query(TripActivity).filter_by(trip_id=tid).one()
                assert s.get(DiscoveryItem, ids["Dar Yacout"]).activity_id == a.id
            page = client.get(f"/trips/{tid}").text
            assert "On timeline · Oct 16, 20:30" in page
            # a forged item id from another trip is ignored
            tid2, _, ids2 = _trip()
            try:
                r = client.post(f"/trips/{tid}/activity", data={
                    "activity_kind": "note", "name": "x", "discovery_item_id": ids2["Kabana"]})
                assert r.headers.get("hx-redirect") == f"/trips/{tid}"
                with SessionLocal() as s:
                    assert s.get(DiscoveryItem, ids2["Kabana"]).activity_id is None
            finally:
                _cleanup(tid2)
        finally:
            _cleanup(tid)

    def test_hide_and_show_a_row():
        tid, fid, ids = _trip()
        try:
            html = client.post(f"/trips/{tid}/discovery-item/{ids['Kabana']}/hide",
                               data={"hidden": "1"}).text
            assert 'id="discovery-section"' in html and "Show hidden (1)" in html
            with SessionLocal() as s:
                i = s.get(DiscoveryItem, ids["Kabana"])
                assert i.hidden is True and i.hidden_reason == "not_my_thing"   # the default
            # the reason picker on the hidden row, with the current reason selected
            assert 'class="input bp-sm yn-hide-reason"' in html
            assert '<option value="not_my_thing" selected>Not my thing</option>' in html
            html = client.post(f"/trips/{tid}/discovery-item/{ids['Kabana']}/hide",
                               data={"hidden": "1", "reason": "closed"}).text
            assert '<option value="closed" selected>' in html
            with SessionLocal() as s:
                assert s.get(DiscoveryItem, ids["Kabana"]).hidden_reason == "closed"
            client.post(f"/trips/{tid}/discovery-item/{ids['Kabana']}/hide",
                        data={"hidden": "1", "reason": "bogus"})
            with SessionLocal() as s:
                assert s.get(DiscoveryItem, ids["Kabana"]).hidden_reason == "not_my_thing"
            client.post(f"/trips/{tid}/discovery-item/{ids['Kabana']}/hide", data={"hidden": "0"})
            with SessionLocal() as s:
                i = s.get(DiscoveryItem, ids["Kabana"])
                assert i.hidden is False and i.hidden_reason is None
            assert client.post(f"/trips/{tid}/discovery-item/nope/hide").status_code == 404
        finally:
            _cleanup(tid)

    def test_edit_summary_edits_the_city_write_up_only():
        tid, fid, ids = _trip()
        try:
            form = client.get(f"/trips/{tid}/finding/{fid}/edit").text
            assert "City summary" in form and "Eat late." in form
            r = client.post(f"/trips/{tid}/finding/{fid}/edit", data={"body": "Eat very late."})
            assert r.headers.get("hx-refresh") == "true"
            with SessionLocal() as s:
                f = s.get(DiscoveryFinding, fid)
                assert f.data["summary"] == "Eat very late." and f.status == "edited"
                assert s.query(DiscoveryItem).filter_by(finding_id=fid).count() == 2
        finally:
            _cleanup(tid)
