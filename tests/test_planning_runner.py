"""Planning runner tests — real DB session, offline geocode, so plan persistence
and the packed-survives-regenerate contract are covered without network.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, PackingItem, PackingPlan, Trip, TripActivity)
    from planning.runner import generate_plan
    Base.metadata.create_all(engine)
    _client = TestClient(app)
    DEPS = True
except ImportError:
    DEPS = False


def _airports(code):
    t = {"YYZ": ("CA", "Toronto", 43.68, -79.63), "GVA": ("CH", "Geneva", 46.23, 6.11)}
    h = t.get((code or "").upper())
    return None if h is None else SimpleNamespace(
        country=h[0], municipality=h[1], lat=h[2], lon=h[3], icao=None)


class _NoGeo:
    def geocode_place(self, p):
        return None

    def geocode_freeform(self, text):
        return None


def _trip():
    with SessionLocal() as s:
        for m in (PackingItem, PackingPlan, TripActivity, Trip):
            s.query(m).delete()
        s.commit()
        t = Trip(name="Alpine trip", start_date="2026-09-01", end_date="2026-09-08")
        s.add(t); s.commit()
        s.add(TripActivity(trip_id=t.id, type="flight",
                           values={"from": "YYZ", "to": "GVA"}))
        s.add(TripActivity(trip_id=t.id, type="lodging",
                           values={"name": "Hotel Lac Bleu", "type": "hotel"}))
        s.commit()
        return t.id


def _gen(tid):
    with SessionLocal() as s:
        return generate_plan(s, s.get(Trip, tid), resolve_airport=_airports,
                             geocoder=_NoGeo()).id


def test_manual_archetypes_persist_and_carry_forward():
    if not DEPS:
        return
    tid = _trip()
    with SessionLocal() as s:
        generate_plan(s, s.get(Trip, tid), resolve_airport=_airports,
                      geocoder=_NoGeo(), manual_archetypes=["cold_ski"])
    with SessionLocal() as s:
        plan = (s.query(PackingPlan).filter_by(trip_id=tid)
                .order_by(PackingPlan.generated_at.desc()).first())
        assert plan.manual_archetypes == ["cold_ski"]
        assert [a["id"] for a in plan.archetypes] == ["cold_ski"]   # override won
        keys = {i.item_key for i in s.query(PackingItem).filter_by(trip_id=tid).all()}
        assert any("insulated_jacket" in k for k in keys)
    # a plain re-generate keeps the choice
    with SessionLocal() as s:
        generate_plan(s, s.get(Trip, tid), resolve_airport=_airports, geocoder=_NoGeo())
    with SessionLocal() as s:
        plan = (s.query(PackingPlan).filter_by(trip_id=tid)
                .order_by(PackingPlan.generated_at.desc()).first())
        assert plan.manual_archetypes == ["cold_ski"]
    # an explicit empty set resets to auto-detection
    with SessionLocal() as s:
        generate_plan(s, s.get(Trip, tid), resolve_airport=_airports,
                      geocoder=_NoGeo(), manual_archetypes=[])
    with SessionLocal() as s:
        plan = (s.query(PackingPlan).filter_by(trip_id=tid)
                .order_by(PackingPlan.generated_at.desc()).first())
        assert plan.manual_archetypes is None


def test_archetype_add_remove_routes(monkeypatch):
    if not DEPS:
        return
    import app.trips.routes as tr
    monkeypatch.setattr(tr, "_airport_resolver", lambda: _airports)
    monkeypatch.setattr(tr, "_photon", lambda: _NoGeo())
    tid = _trip()
    with SessionLocal() as s:                        # seed an initial (auto) plan
        generate_plan(s, s.get(Trip, tid), resolve_airport=_airports, geocoder=_NoGeo())
    r = _client.post(f"/trips/{tid}/pack/archetypes/add", data={"archetype_id": "cold_ski"})
    assert r.status_code == 200 and "Insulated jacket" in r.text
    with SessionLocal() as s:
        plan = (s.query(PackingPlan).filter_by(trip_id=tid)
                .order_by(PackingPlan.generated_at.desc()).first())
        assert "cold_ski" in (plan.manual_archetypes or [])
    r = _client.post(f"/trips/{tid}/pack/archetypes/remove", data={"archetype_id": "cold_ski"})
    assert r.status_code == 200 and "Insulated jacket" not in r.text


def test_generate_persists_a_plan_with_essentials():
    if not DEPS:
        return
    tid = _trip()
    _gen(tid)
    with SessionLocal() as s:
        plan = s.query(PackingPlan).filter_by(trip_id=tid).one()
        assert plan.status == "done"
        assert plan.archetypes                       # something was chosen
        items = s.query(PackingItem).filter_by(trip_id=tid).all()
        keys = {i.item_key for i in items}
        assert "documents:passport" in keys
        # CH is international vs an empty home set → international essentials in
        assert "documents:travel_insurance" in keys
        # no weather findings → a heads-up warning
        assert any("weather" in w.lower() for w in (plan.warnings or []))


def test_packed_item_survives_regenerate():
    if not DEPS:
        return
    tid = _trip()
    _gen(tid)
    with SessionLocal() as s:
        pi = s.query(PackingItem).filter_by(trip_id=tid,
                                            item_key="documents:passport").one()
        pi.status = "packed"
        s.commit()
        pid = pi.id
    _gen(tid)                                        # re-generate
    with SessionLocal() as s:
        assert s.get(PackingItem, pid).status == "packed"   # not reset to draft
        # exactly one passport row — upserted, not duplicated
        assert s.query(PackingItem).filter_by(
            trip_id=tid, item_key="documents:passport").count() == 1


def test_trip_detail_renders_the_packing_plan():
    """Render smoke — catches template traps (e.g. Jinja `grp.items` resolving to
    a dict method) that unit tests on the data alone miss."""
    if not DEPS:
        return
    tid = _trip()
    _gen(tid)
    r = _client.get(f"/trips/{tid}")
    assert r.status_code == 200
    assert "Packing" in r.text and "yn-pack-row" in r.text


def test_packing_profile_form_is_structured_not_json():
    if not DEPS:
        return
    r = _client.get("/trips/settings/packing-profile")
    assert r.status_code == 200
    assert '<select' in r.text                          # dropdowns, not a textarea
    for name in ("bag_mode", "laundry_comfort", "packing_style",
                 "cultural_strictness", "style_constraints", "must_have_items"):
        assert f'name="{name}"' in r.text


def test_packing_profile_saves_structured_values():
    if not DEPS:
        return
    from app.models import PackingProfile
    with SessionLocal() as s:
        s.query(PackingProfile).delete()
        s.commit()
    try:
        r = _client.post("/trips/settings/packing-profile", data={
            "bag_mode": "one_bag", "bag_weight_kg": "8",
            "laundry_comfort": "machine", "packing_style": "minimalist",
            "climate_preference": "runs_hot", "cultural_strictness": "flexible",
            "clothing_variant": "neutral",
            "style_constraints": ["no_heels", "modest_dress"],
            "must_have_items": ["headphones"]})
        assert r.status_code == 200 and "saved" in r.text.lower()
        with SessionLocal() as s:
            v = s.query(PackingProfile).one().values
            assert v["bag"] == {"mode": "one_bag", "weight_kg": 8}
            assert v["laundry_comfort"] == "machine"
            assert set(v["style_constraints"]) == {"no_heels", "modest_dress"}
            assert v["must_have_items"] == ["headphones"]
    finally:
        with SessionLocal() as s:                        # don't pollute defaults
            s.query(PackingProfile).delete()
            s.commit()


def test_check_off_is_inline_not_a_reload():
    """pack returns just the row + an OOB count, with no HX-Redirect — so the page
    doesn't reload / jump (D-85)."""
    if not DEPS:
        return
    tid = _trip()
    _gen(tid)
    with SessionLocal() as s:
        iid = s.query(PackingItem).filter_by(
            trip_id=tid, item_key="documents:passport").one().id
    r = _client.post(f"/trips/{tid}/pack-item/{iid}/pack")
    assert r.status_code == 200
    assert "HX-Redirect" not in r.headers          # not a full-page navigation
    assert "yn-pack-row" in r.text                 # just the row
    assert 'hx-swap-oob="true"' in r.text          # + the category count
    assert "<html" not in r.text.lower()           # a fragment, not a page
    with SessionLocal() as s:
        assert s.get(PackingItem, iid).status == "packed"
