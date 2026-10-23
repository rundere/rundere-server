"""YONDER-200 slice 3a: Photon enrichment of a Maps place link, and the in-trip
"add by link" form (D-168). Photon is stubbed — nothing here touches the network."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from resolve import place_lookup

PIN = (40.7615, -73.9817)


class StubPhoton:
    def __init__(self, features):
        self.features, self.calls = features, []

    def _query(self, q, limit=5, near=None):
        self.calls.append((q, near))
        return self.features


def _feat(name, lat, lon, **props):
    return {"geometry": {"coordinates": [lon, lat]},
            "properties": {"name": name, **props}}


BERNARDIN = _feat("Le Bernardin", 40.7616, -73.9818, street="West 51st Street",
                  housenumber="155", city="New York", countrycode="US",
                  osm_key="amenity", osm_value="restaurant")


def test_enrich_returns_address_and_osm_category_for_a_verified_hit():
    ph = StubPhoton([BERNARDIN])
    got = place_lookup.enrich("Le Bernardin", *PIN, geocoder=ph)
    assert got == {"address": "155 West 51st Street, New York",
                   "category": "amenity=restaurant"}
    assert ph.calls == [("Le Bernardin", PIN)]          # biased to the link's own pin


def test_enrich_puts_the_number_after_the_street_outside_number_first_countries():
    f = _feat("Café Central", 48.2, 16.36, street="Herrengasse", housenumber="14",
              city="Wien", countrycode="AT", osm_key="amenity", osm_value="cafe")
    got = place_lookup.enrich("Café Central", 48.2, 16.36, geocoder=StubPhoton([f]))
    assert got["address"] == "Herrengasse 14, Wien"


def test_enrich_rejects_the_wrong_branch():
    """The documented Photon trap: a same-named place far from the pin is not it."""
    far = _feat("Le Bernardin", 48.85, 2.35, street="Rue X", city="Paris")
    assert place_lookup.enrich("Le Bernardin", *PIN, geocoder=StubPhoton([far])) == {}


def test_enrich_rejects_a_nearby_place_with_a_different_name():
    other = _feat("Joe's Pizza", 40.7616, -73.9818, osm_key="amenity", osm_value="fast_food")
    assert place_lookup.enrich("Le Bernardin", *PIN, geocoder=StubPhoton([other])) == {}


def test_enrich_picks_the_nearest_verified_hit():
    near = _feat("Le Bernardin", 40.76152, -73.98171, street="A", osm_key="amenity",
                 osm_value="restaurant")
    further = _feat("Le Bernardin", 40.7630, -73.9817, street="B", osm_key="amenity",
                    osm_value="bar")
    got = place_lookup.enrich("Le Bernardin", *PIN, geocoder=StubPhoton([further, near]))
    assert got["category"] == "amenity=restaurant"


def test_enrich_needs_a_name_and_a_pin():
    ph = StubPhoton([BERNARDIN])
    assert place_lookup.enrich("Le Bernardin", None, None, geocoder=ph) == {}
    assert place_lookup.enrich(None, *PIN, geocoder=ph) == {}
    assert ph.calls == []                                # name-only is never guessed at


def test_enrich_is_off_without_photon_and_never_raises(monkeypatch):
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: None)
    assert place_lookup.enrich("Le Bernardin", *PIN) == {}

    class Boom:
        def _query(self, *a, **k):
            raise RuntimeError("down")
    assert place_lookup.enrich("Le Bernardin", *PIN, geocoder=Boom()) == {}


# ------------------------------------------------------------- places service
try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import places as places_svc
    from app.trips import types as activity_types
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, SavedPlace, Trip, TripActivity

    Base.metadata.create_all(engine)
    client = TestClient(app)
    A_Q = "https://maps.apple.com/?q=Le+Bernardin&ll=40.7615,-73.9817"

    def test_resolve_fields_enriches_only_the_gaps():
        ph = StubPhoton([BERNARDIN])
        f = places_svc.resolve_fields(url=A_Q, enrich=True, geocoder=ph)
        assert f["address"].startswith("155 West 51st") and f["activity_type"] == "dining"
        # what the client already resolved always wins over Photon
        g = places_svc.resolve_fields(url=A_Q, address="Mine", category="MKPOICategoryMuseum",
                                      enrich=True, geocoder=StubPhoton([BERNARDIN]))
        assert g["address"] == "Mine" and g["activity_type"] == "tour"

    def test_resolve_fields_does_not_look_up_unless_asked():
        ph = StubPhoton([BERNARDIN])
        f = places_svc.resolve_fields(url=A_Q, geocoder=ph)
        assert ph.calls == [] and f["address"] is None and f["activity_type"] == "tour"

    def test_activity_from_link_prefills_type_and_keeps_the_link(monkeypatch):
        monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: StubPhoton([BERNARDIN]))
        atype, values = places_svc.activity_from_link(A_Q)
        assert atype == "dining" and values["name"] == "Le Bernardin"
        assert values["address"].startswith("155 West 51st") and values["mapLink"] == A_Q
        assert "when" not in values                         # the time is the person's to set

    def test_activity_from_link_refuses_non_place_links(monkeypatch):
        monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: None)
        for bad in ("https://www.google.com/maps/dir/A/B/@1,2,3z", "hello",
                    "https://example.com/maps/place/X"):
            try:
                places_svc.activity_from_link(bad)
            except ValueError:
                continue
            raise AssertionError(f"accepted {bad!r}")

    # ---------------------------------------------------------------- the web form
    def _trip():
        with SessionLocal() as s:
            t = Trip(name="NYC", source="manual")
            s.add(t)
            s.commit()
            s.refresh(t)
            return t.id

    def _drop(trip_id):
        with SessionLocal() as s:
            s.query(TripActivity).filter_by(trip_id=trip_id).delete()
            s.query(Trip).filter_by(id=trip_id).delete()
            s.commit()

    def test_new_activity_form_offers_the_maps_link_field():
        tid = _trip()
        try:
            r = client.get(f"/trips/{tid}/activity/new")
            assert r.status_code == 200 and r.text.count('name="mapLink"') == 1   # one field (D-209)
            assert 'name="link"' not in r.text
        finally:
            _drop(tid)

    def test_from_link_prefills_the_form_with_the_suggested_type(monkeypatch):
        monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: StubPhoton([BERNARDIN]))
        tid = _trip()
        try:
            r = client.get(f"/trips/{tid}/activity/from-link", params={"mapLink": A_Q})
            assert r.status_code == 200, r.text
            assert '<option value="dining" selected>' in r.text
            assert "Le Bernardin" in r.text and "155 West 51st" in r.text
            assert "Filled in from your link" in r.text
            assert client.get(f"/trips/{tid}/activity/new").text.count("Le Bernardin") == 0
            with SessionLocal() as s:                       # nothing saved by looking
                assert s.query(TripActivity).filter_by(trip_id=tid).count() == 0
        finally:
            _drop(tid)

    def test_from_link_with_a_bad_link_shows_an_error_and_a_blank_form(monkeypatch):
        monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: None)
        tid = _trip()
        try:
            r = client.get(f"/trips/{tid}/activity/from-link", params={"mapLink": "nope"})
            assert r.status_code == 200 and "isn&#39;t a Google or Apple Maps place link" in r.text
        finally:
            _drop(tid)

    LARKSPUR = _feat("Larkspur Inn", 35.0841, -106.6509, street="Ocotillo Lane",
                     housenumber="12", city="Cottonwood Springs", countrycode="US",
                     osm_key="amenity", osm_value="restaurant")
    L_Q = "https://maps.apple.com/?q=Larkspur+Inn&ll=35.0840,-106.6508"

    def test_fill_in_keeps_what_the_person_entered(monkeypatch):
        """YONDER-315: a lodging half filled in, then a Maps link. The link said
        "restaurant", but the person's type, name and dates stay; only the empty
        address and the Map link come from the link (D-209)."""
        monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: StubPhoton([LARKSPUR]))
        tid = _trip()
        try:
            r = client.get(f"/trips/{tid}/activity/from-link", params={
                "mapLink": L_Q, "activity_kind": "lodging", "name": "Cottonwood Lodge",
                "when": "2031-05-14T15:00", "endWhen": "2031-05-17T11:00",
                "confirmation": "CW-4471"})
            assert r.status_code == 200, r.text
            assert '<option value="lodging" selected>' in r.text
            assert 'value="Cottonwood Lodge"' in r.text and "Larkspur Inn" not in r.text
            assert "2031-05-14T15:00" in r.text and "2031-05-17T11:00" in r.text
            assert "CW-4471" in r.text
            assert "12 Ocotillo Lane" in r.text                          # the empty address
            assert r.text.count('name="mapLink"') == 1 and "maps.apple.com" in r.text
            assert "entered is kept" in r.text
        finally:
            _drop(tid)

    def test_a_link_suggests_a_type_only_when_none_is_chosen(monkeypatch):
        """D-209 amends D-168: the link's type applies to an untouched form."""
        monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: StubPhoton([LARKSPUR]))
        tid = _trip()
        try:
            r = client.get(f"/trips/{tid}/activity/from-link",
                           params={"mapLink": L_Q, "activity_kind": ""})
            assert '<option value="dining" selected>' in r.text
            assert 'value="Larkspur Inn"' in r.text and "12 Ocotillo Lane" in r.text
            r = client.get(f"/trips/{tid}/activity/from-link",
                           params={"mapLink": L_Q, "activity_kind": "tour"})
            assert '<option value="tour" selected>' in r.text               # chosen: kept
            assert 'value="Larkspur Inn"' in r.text                         # empty name filled
            assert "12 Ocotillo Lane" in r.text                             # into "location"
        finally:
            _drop(tid)

    def test_a_bad_link_keeps_the_form_as_it_was(monkeypatch):
        monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: None)
        tid = _trip()
        try:
            r = client.get(f"/trips/{tid}/activity/from-link", params={
                "mapLink": "nope", "activity_kind": "lodging", "name": "Cottonwood Lodge"})
            assert "isn&#39;t a Google or Apple Maps place link" in r.text
            assert '<option value="lodging" selected>' in r.text
            assert 'value="Cottonwood Lodge"' in r.text
        finally:
            _drop(tid)

    def test_new_activity_has_no_default_type_and_one_map_link():
        """D-209: the form starts on "Choose a type…", sends the whole form when a
        link is pasted, and has a single Map link field."""
        tid = _trip()
        try:
            html = client.get(f"/trips/{tid}/activity/new").text
            assert '<option value="" selected>Choose a type…</option>' in html
            assert '<option value="flight" selected>' not in html
            assert html.count('name="mapLink"') == 1
            assert 'hx-include="[name=link]"' not in html
            assert html.count('hx-include="closest form"') >= 2          # paste and Fill in
        finally:
            _drop(tid)

    def test_saving_without_a_type_is_refused_not_a_flight():
        tid = _trip()
        try:
            r = client.post(f"/trips/{tid}/activity", data={"activity_kind": "",
                                                             "mapLink": L_Q})
            assert "Choose a type for this activity." in r.text and L_Q.split("?")[0] in r.text
            with SessionLocal() as s:
                assert s.query(TripActivity).filter_by(trip_id=tid).count() == 0
        finally:
            _drop(tid)

    def test_saving_fills_a_place_activitys_blanks_from_its_map_link(monkeypatch):
        """YONDER-318: on save, a place link fills an empty address for any type,
        not only a road trip's From/To."""
        monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: StubPhoton([LARKSPUR]))
        from app.trips.enrich import enrich_activity_values
        out = enrich_activity_values("lodging", {"mapLink": L_Q, "name": "Cottonwood Lodge"})
        assert out["name"] == "Cottonwood Lodge" and "12 Ocotillo Lane" in out["address"]
        typed = {"mapLink": L_Q, "name": "Mine", "address": "Typed by me"}
        assert enrich_activity_values("dining", typed) == typed           # nothing replaced

    def test_a_directions_link_suggests_a_road_trip_and_fills_its_stops(monkeypatch):
        from resolve import routing
        stops = [{"raw": "Cottonwood Lodge", "name": "Cottonwood Lodge", "lat": 35.08, "lon": -106.65},
                 {"raw": "Ocotillo Mesa, NM", "name": "Ocotillo Mesa, NM", "city": "Ocotillo Mesa",
                  "region": "NM", "lat": 35.40, "lon": -106.10}]
        monkeypatch.setattr(routing, "estimate_for_link",
                            lambda url, geocoder, base_url: (stops, routing.RouteEstimate(80.0, 60)))
        tid = _trip()
        try:
            r = client.get(f"/trips/{tid}/activity/from-link", params={
                "mapLink": "https://www.google.com/maps/dir/Cottonwood+Lodge/Ocotillo+Mesa,+NM",
                "activity_kind": ""})
            assert '<option value="drive" selected>' in r.text
            assert 'value="Cottonwood Lodge"' in r.text and "Ocotillo Mesa, NM" in r.text
            assert "80 km" in r.text
        finally:
            _drop(tid)

    def test_from_link_404s_for_an_unknown_trip():
        assert client.get("/trips/nope/activity/from-link", params={"mapLink": A_Q}).status_code == 404
