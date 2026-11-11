"""The trip PDF's sections and addresses (SPEC-8 §13, D-233, YONDER-311).

Everything here is fictional: example names, example.test hosts, invented
codes. Maps are switched off or schematic, so nothing touches the network.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

try:
    import sqlalchemy  # noqa: F401
    DEPS = True
except ImportError:
    DEPS = False

pytestmark = pytest.mark.skipif(not DEPS, reason="needs the app's dependencies")

if DEPS:
    from app.db import SessionLocal, engine
    from app.models import (Base, DiscoveryFinding, PackingItem, Trip, TripActivity,
                            TravelerIdentity, TripTraveler, User)
    from app.trips import geo
    from app.trips import pdf
    from app.trips.pdf import Sections, render_timeline_html, sections_from_query, timeline_context
    Base.metadata.create_all(engine)

YYZ, PHX = (43.6777, -79.6248), (33.4342, -112.0116)


def _airport_pt(role, code, lat, lon, address):
    return {"role": role, "label": code, "lat": lat, "lon": lon, "source": "airport",
            "address": address}


@pytest.fixture
def trip(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "map_tile_url", "")
    with SessionLocal() as s:
        ta = TravelerIdentity(display_name="Example Ada PDF", kind="guest")
        tb = TravelerIdentity(display_name="Example Ben PDF", kind="guest")
        s.add_all([ta, tb]); s.flush()
        a = User(username="pdf-sections-ada", auth_source="local", traveler_id=ta.id)
        b = User(username="pdf-sections-ben", auth_source="local", traveler_id=tb.id)
        s.add_all([a, b]); s.flush()
        t = Trip(name="PDF sections test", start_date="2031-05-01", end_date="2031-05-04",
                 owner_id=a.id)
        s.add(t); s.flush()
        s.add_all([TripTraveler(trip_id=t.id, traveler_id=ta.id, role="owner"),
                   TripTraveler(trip_id=t.id, traveler_id=tb.id, role="companion")])
        s.add_all([
            TripActivity(trip_id=t.id, type="flight", starts_at="2031-05-01T08:15",
                         values={"when": "2031-05-01T08:15", "from": "YYZ", "to": "PHX",
                                 "confirmation": "EXMPL1"},
                         geo={"points": [
                             _airport_pt("from", "YYZ", *YYZ, "Example Intl Airport, Toronto, CA"),
                             _airport_pt("to", "PHX", *PHX, "Example Sky Airport, Phoenix, US")]}),
            TripActivity(trip_id=t.id, type="lodging", starts_at="2031-05-01T15:00",
                         values={"when": "2031-05-01T15:00", "name": "Hotel Example",
                                 "address": "12 Example Way, Phoenix",
                                 "notes": "Door code 4242"},
                         geo={"points": [{"role": "place", "label": "12 Example Way, Phoenix",
                                          "lat": 33.47, "lon": -112.05, "source": "photon",
                                          "address": "Example Way 12, Phoenix, US"}]}),
            TripActivity(trip_id=t.id, type="dining", starts_at="2031-05-02T19:30",
                         values={"when": "2031-05-02T19:30", "name": "Example Bistro"},
                         geo={"points": [{"role": "place", "label": "Example Bistro",
                                          "lat": 33.45, "lon": -112.07, "source": "photon",
                                          "address": "Sample Street 7, Phoenix, US"}]}),
            TripActivity(trip_id=t.id, type="taxi", starts_at="2031-05-03T09:00",
                         values={"when": "2031-05-03T09:00", "company": "Example Cabs",
                                 "from": "12 Example Way", "to": "40 Sample Road"}),
        ])
        s.commit()
        ids = {"trip": t.id, "a": a.id, "b": b.id}
    yield ids
    with SessionLocal() as s:
        s.query(TripActivity).filter(TripActivity.trip_id == ids["trip"]).delete()
        s.query(PackingItem).filter(PackingItem.trip_id == ids["trip"]).delete()
        s.query(DiscoveryFinding).filter(DiscoveryFinding.trip_id == ids["trip"]).delete()
        s.query(TripTraveler).filter(TripTraveler.trip_id == ids["trip"]).delete()
        for u in s.query(User).filter(User.id.in_([ids["a"], ids["b"]])).all():
            tid = u.traveler_id
            s.delete(u)
            s.flush()
            if tid:
                s.delete(s.get(TravelerIdentity, tid))
        s.delete(s.get(Trip, ids["trip"]))
        s.commit()


def _html(ids, sections=None, who="a"):
    with SessionLocal() as s:
        user = s.get(User, ids[who]) if who else None
        return render_timeline_html(s, s.get(Trip, ids["trip"]), sections, user)


def _ctx(ids, sections=None):
    with SessionLocal() as s:
        return timeline_context(s, s.get(Trip, ids["trip"]), sections)


def _acts(ctx):
    return {a["type"]: a for d in ctx["days"] for a in d["activities"]}


# ------------------------------------------------------------------ the flags

def test_a_link_without_flags_keeps_the_old_pdf():
    assert sections_from_query({}) == Sections()
    assert sections_from_query({"times": "false"}) == Sections(times=False)


def test_the_form_sends_only_ticked_boxes():
    from starlette.datastructures import QueryParams
    got = sections_from_query(QueryParams("pick=1&maps=1&packing=1"))
    assert got == Sections(times=False, addresses=False, maps=True, details=False,
                           packing=True, discover=False, topics=frozenset())


def test_discover_topics_come_as_ticked_boxes():
    from starlette.datastructures import QueryParams
    got = sections_from_query(QueryParams("pick=1&discover=1&topic=plugs&topic=food&topic=nope"))
    assert got.discover and got.topics == frozenset({"plugs", "food"})
    # A link that names no topic gets them all.
    assert sections_from_query(QueryParams("discover=1")).topics is None


# ------------------------------------------------------------------ what each one leaves out

def test_by_default_times_addresses_and_details_print(trip):
    html = _html(trip)
    assert "8:15" in html and "EXMPL1" in html and "Door code 4242" in html
    assert "12 Example Way, Phoenix" in html
    # the found address where nothing was typed, labelled as found
    assert "Address (from the map)" in html and "Sample Street 7, Phoenix, US" in html
    # the typed lodging address wins over Photon's version of it
    assert "Example Way 12, Phoenix, US" not in html
    # airports, from the bundled table, not marked as found
    assert "From address" in html and "Example Intl Airport, Toronto, CA" in html
    assert "From address (from the map)" not in html


def test_no_times_keeps_the_days_but_not_when(trip):
    ctx = _ctx(trip, Sections(times=False))
    acts = _acts(ctx)
    assert all(a["time_label"] == "" for a in acts.values())
    assert not any(r["label"] in ("Departure", "Check-in", "Check-out", "Reservation")
                   for a in acts.values() for r in a["rows"])
    html = _html(trip, Sections(times=False))
    assert "8:15" not in html and "7:30" not in html
    assert "Hotel Example" in html and "Example Bistro" in html


def test_no_addresses_leaves_out_typed_found_and_airport_addresses(trip):
    html = _html(trip, Sections(addresses=False))
    for text in ("12 Example Way", "40 Sample Road", "Sample Street 7",
                 "Example Intl Airport", "Address (from the map)"):
        assert text not in html, text
    # the itinerary stays: the flight's airports by code, the places by name
    assert "YYZ" in html and "Hotel Example" in html and "Example Cabs" in html


def test_no_details_leaves_out_codes_and_notes(trip):
    html = _html(trip, Sections(details=False))
    assert "EXMPL1" not in html and "Door code 4242" not in html
    assert "Hotel Example" in html and "8:15" in html


def test_no_maps_draws_none_and_numbers_nothing(trip):
    ctx = _ctx(trip, Sections(maps=False))
    assert ctx["trip_map"] is None and ctx["flight_map"] is None
    assert not any(a.get("map_no") for a in _acts(ctx).values())
    with_maps = _ctx(trip)
    assert with_maps["trip_map"] and with_maps["flight_map"]


# ------------------------------------------------------------------ personal sections

def _pack(ids, who, label, status="draft"):
    with SessionLocal() as s:
        s.add(PackingItem(trip_id=ids["trip"], user_id=ids[who], item_key=f"x:{label}",
                          label=label, category="clothing", quantity=2, bag="carry_on",
                          status=status))
        s.commit()


def test_packing_is_the_downloaders_own_list(trip):
    _pack(trip, "a", "Example rain shell")
    _pack(trip, "a", "Example spare socks", status="packed")
    _pack(trip, "a", "Example suggested hat", status="suggested")
    _pack(trip, "b", "Ben's example scarf")
    assert "My packing list" not in _html(trip)                     # off by default
    html = _html(trip, Sections(packing=True))
    assert "My packing list" in html
    assert "2 × Example rain shell" in html and "Example spare socks" in html
    assert "Example suggested hat" not in html
    assert "Ben&#39;s example scarf" not in html and "Ben's example scarf" not in html
    ben = _html(trip, Sections(packing=True), who="b")
    assert "example scarf" in ben and "Example rain shell" not in ben


def test_briefing_is_the_downloaders_own_research(trip):
    with SessionLocal() as s:
        s.add_all([
            DiscoveryFinding(trip_id=trip["trip"], user_id=trip["a"], topic="plugs",
                             scope="country", place_key="US", status="draft",
                             body_md="Example plugs: type A and B, 120 V."),
            DiscoveryFinding(trip_id=trip["trip"], user_id=trip["a"], topic="visa",
                             scope="country", place_key="US", status="rejected",
                             body_md="Example rejected visa note."),
            DiscoveryFinding(trip_id=trip["trip"], user_id=trip["a"], topic="food",
                             scope="city", place_key="US:Phoenix", status="draft",
                             body_md="Example food write-up."),
            DiscoveryFinding(trip_id=trip["trip"], user_id=trip["b"], topic="safety",
                             scope="country", place_key="US", status="draft",
                             body_md="Ben's example safety note."),
        ])
        s.commit()
    html = _html(trip, Sections(discover=True))
    assert "<h2>Discover</h2>" in html and "Example plugs: type A and B" in html
    assert "Example food write-up" in html               # every topic, unless unticked
    assert "Example rejected visa note" not in html
    assert "safety note" not in html
    only = _html(trip, Sections(discover=True, topics=frozenset({"plugs"})))
    assert "Example plugs" in only and "Example food write-up" not in only
    assert "<h2>Discover</h2>" not in _html(trip, Sections(discover=True,
                                                          topics=frozenset()))
    # The form offers a box per topic Ada has something in; not Ben's, not rejected.
    with SessionLocal() as s:
        offered = [t["key"] for t in pdf.briefing_topics(s, trip["trip"], s.get(User, trip["a"]))]
    assert offered == ["plugs", "food"]


def test_briefing_places_read_as_words():
    assert pdf._place("FR:Nice") == "Nice, FR" and pdf._place("FR") == "FR"


def test_a_viewer_gets_neither_packing_nor_briefing(trip, monkeypatch):
    _pack(trip, "a", "Example rain shell")
    from app import scope
    monkeypatch.setattr(scope, "can_edit_trip", lambda *a, **k: False)
    html = _html(trip, Sections(packing=True, discover=True))
    assert "My packing list" not in html and "Example rain shell" not in html


# ------------------------------------------------------------------ addresses on pins

class _Geocoder:
    def geocode_place(self, place):
        from resolve.geocode import GeocodeResult
        return GeocodeResult(lat=33.45, lon=-112.07, country="US", city="Phoenix",
                             name=place.get("raw"), confidence="verified",
                             address="Sample Street 7")

    def geocode_freeform(self, text):
        return None


class _Airport:
    lat, lon = YYZ
    name, municipality, country = "Example Intl Airport", "Toronto", "CA"


def test_locating_keeps_the_address_it_was_given():
    loc = geo.Locator(geocoder=_Geocoder(), airport=lambda code: _Airport(), route_url="")
    g = geo.locate_activity("dining", {"name": "Example Bistro"}, locator=loc)
    assert g["points"][0]["address"] == "Sample Street 7, Phoenix, US"
    f = geo.locate_activity("flight", {"from": "YYZ", "to": "YYZ"}, locator=loc)
    assert f["points"][0]["address"] == "Example Intl Airport, Toronto, CA"


def test_a_pin_from_before_addresses_is_located_again_once():
    values = {"name": "Example Bistro"}
    key = geo.fingerprint("dining", values)
    old = {"points": [{"role": "place", "label": "Example Bistro", "lat": 1.0, "lon": 2.0,
                       "source": "photon"}], "key": key, "complete": True}
    assert not geo._reusable(old, key)
    old["points"][0]["address"] = None                   # looked up, none found
    assert geo._reusable(old, key)
    link = {"points": [{"role": "place", "label": "Example Bistro", "lat": 1.0, "lon": 2.0,
                        "source": "link"}], "key": key, "complete": True}
    assert geo._reusable(link, key)                      # a link pin never had one


def test_the_api_lists_the_flags():
    from app.main import app
    op = app.openapi()["paths"]["/api/v1/trips/{trip_id}/timeline.pdf"]["get"]
    names = {p["name"] for p in op["parameters"]}
    assert {"times", "addresses", "maps", "details", "packing", "discover", "topic"} <= names
    assert pdf.Sections().packing is False


# ------------------------------------------------------------------ the upgrade step

def test_upgrade_locates_old_pins_once(monkeypatch):
    from app import upgrade
    from app.models import ServerSetting
    calls = []
    monkeypatch.setattr(upgrade, "STEPS", (("test-step", lambda s: calls.append(1) or "ok"),))
    with SessionLocal() as s:
        s.query(ServerSetting).filter(ServerSetting.key == "upgrade:test-step").delete()
        s.commit()
        assert upgrade.pending(s) == ["test-step"]
    assert upgrade.run(SessionLocal) == {"test-step": "ok"}
    assert upgrade.run(SessionLocal) == {} and calls == [1]       # done means done
    with SessionLocal() as s:
        assert upgrade.pending(s) == []


def test_a_failed_upgrade_step_runs_again(monkeypatch):
    from app import upgrade
    from app.models import ServerSetting

    def boom(session):
        raise RuntimeError("example failure")
    monkeypatch.setattr(upgrade, "STEPS", (("test-boom", boom),))
    with SessionLocal() as s:
        s.query(ServerSetting).filter(ServerSetting.key == "upgrade:test-boom").delete()
        s.commit()
    assert upgrade.run(SessionLocal) == {"test-boom": "failed (RuntimeError)"}
    with SessionLocal() as s:
        assert upgrade.pending(s) == ["test-boom"]


def test_the_address_step_is_the_locate_backfill(monkeypatch):
    from app import upgrade
    from app.trips import geo as geo_mod
    monkeypatch.setattr(geo_mod, "backfill", lambda session: {"located": 2, "skipped": 5})
    assert dict(upgrade.STEPS)["locate-addresses"](None) == "located=2, skipped=5"
