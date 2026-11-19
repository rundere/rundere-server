"""An event's venue survives capture → timeline → Discover whole.

A captured happy hour at "Quincy Hall, 4001 Fairfax Dr, Arlington, VA" was
committed as a bare "Arlington" (the commit took the place's city first), and
Discover then looked that up by name and filed the trip under Arlington, Texas,
labelled by its county ("Tarrant County"). Each step is covered here, offline.
All places and venues are fictional or generic.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.committing import build_yonder_activity  # noqa: E402
from discover.places import derive_locations  # noqa: E402
from resolve.geocode import GeocodeResult, _feature_city  # noqa: E402


def _event(venue: dict) -> dict:
    return {"item_type": "event",
            "event": {"title": "Happy Hour", "venue": venue,
                      "start": {"local": "2026-11-20T18:30", "precision": "datetime"}}}


# ------------------------------------------------------------------ commit

def test_event_keeps_venue_name_and_full_address():
    _, values = build_yonder_activity(_event({
        "name": "Example Hall", "address": "100 Sample Street", "city": "Arlington",
        "region": "VA", "postcode": "22203", "country": "US"}))
    assert values["venue"] == "Example Hall"
    assert values["address"] == "100 Sample Street, Arlington, VA 22203"


def test_event_venue_that_is_a_full_address_splits_name_from_address():
    whole = "Example Hall, 100 Sample St, Arlington, VA 22203, USA"
    _, values = build_yonder_activity(_event({
        "name": whole, "raw": whole, "address": "100 Sample Street",
        "city": "Arlington", "region": "VA"}))
    assert values["venue"] == "Example Hall"
    assert values["address"] == "100 Sample St, Arlington, VA 22203, USA"


def test_event_with_only_a_city_has_an_address_of_city_and_state():
    _, values = build_yonder_activity(_event({"city": "Lynchburg", "region": "VA"}))
    assert "venue" not in values
    assert values["address"] == "Lynchburg, VA"


def test_event_venue_name_only_is_unchanged():
    _, values = build_yonder_activity(_event({"name": "Example Arena"}))
    assert values["venue"] == "Example Arena" and "address" not in values


def test_lodging_address_keeps_city_state_and_postcode():
    _, values = build_yonder_activity({"item_type": "lodging", "lodging": {
        "property_name": "Example Inn", "place": {
            "name": "Example Inn", "address": "5 Sample Road", "city": "Forest",
            "region": "VA", "postcode": "24551"}}})
    assert values["address"] == "5 Sample Road, Forest, VA 24551"


def test_event_subtitle_and_map_search_use_venue_and_address():
    from app.trips.types import get_type, map_query
    v = {"name": "Happy Hour", "venue": "Example Hall", "address": "100 Sample St, Arlington, VA"}
    assert get_type("event").subtitle(v) == "Example Hall · 100 Sample St, Arlington, VA"
    assert map_query("event", v) == "Example Hall, 100 Sample St, Arlington, VA"


def test_event_pin_fingerprint_unchanged_until_an_address_is_added():
    from app.trips.geo import fingerprint
    old = {"name": "Happy Hour", "venue": "Arlington"}
    assert fingerprint("event", old) == fingerprint("event", {**old, "address": ""})
    assert fingerprint("event", old) != fingerprint("event", {**old, "address": "1 Sample St"})


# ------------------------------------------------------------------ Discover

class _Geocoder:
    """Forward lookups land in Texas, as Photon's top hit for "Arlington" does;
    the reverse lookup knows the stored point is in Virginia."""
    def __init__(self):
        self.reversed = []

    def geocode_place(self, place):
        return None

    def geocode_freeform(self, text):
        return GeocodeResult(lat=32.7, lon=-97.1, country="US", city="Tarrant County",
                             name="Arlington", confidence="verified")

    def reverse(self, lat, lon):
        self.reversed.append((lat, lon))
        return {"locality": "Arlington", "region": "VA", "country_code": "US"}


def _act(values, geo=None):
    return SimpleNamespace(id="a1", type="event", values=values, geo=geo)


def test_discover_uses_the_point_already_on_the_map():
    geo = {"points": [{"role": "place", "label": "Example Hall",
                       "lat": 38.88, "lon": -77.10}], "complete": True}
    g = _Geocoder()
    out = derive_locations([_act({"name": "Happy Hour", "venue": "Arlington"}, geo)],
                           resolve_airport=lambda c: None, geocoder=g)
    assert [(p.city, p.country, p.region) for p in out.places] == [("Arlington", "US", "VA")]
    assert g.reversed == [(38.88, -77.10)]


def test_discover_without_a_stored_point_still_falls_back_to_lookup():
    out = derive_locations([_act({"name": "Happy Hour", "venue": "Arlington"})],
                           resolve_airport=lambda c: None, geocoder=_Geocoder())
    assert [p.city for p in out.places] == ["Tarrant County"]


def test_discover_ignores_a_failed_reverse_lookup():
    g = _Geocoder()
    g.reverse = lambda lat, lon: None          # Photon down (D-146)
    geo = {"points": [{"role": "place", "lat": 38.88, "lon": -77.10}]}
    out = derive_locations([_act({"venue": "Arlington"}, geo)],
                           resolve_airport=lambda c: None, geocoder=g)
    assert [p.city for p in out.places] == ["Tarrant County"]


# ------------------------------------------------------------------ labels

def test_a_town_feature_is_named_by_itself_not_its_county():
    props = {"type": "city", "name": "Arlington", "county": "Tarrant County",
             "state": "Texas", "countrycode": "US"}
    assert _feature_city(props) == "Arlington"


def test_a_street_feature_still_uses_its_city_then_county():
    assert _feature_city({"type": "house", "name": "Example Hall",
                          "city": "Springfield"}) == "Springfield"
    assert _feature_city({"type": "house", "name": "Example Hall",
                          "county": "Example County"}) == "Example County"


# ------------------------------------------------------------------ review form

def test_review_form_shows_the_venue_state_and_postcode():
    from app.review.routes import _fields
    paths = {f["path"] for f in _fields(_event({"city": "Arlington", "region": "VA"}))}
    assert {"/event/venue/region", "/event/venue/postcode"} <= paths


def test_schemas_and_llm_grammar_carry_postcode():
    import json
    for name in ("travel-item", "travel-extraction"):
        s = json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text())
        assert '"postcode"' in json.dumps(s), name
    from extract import llm
    assert "place.postcode" in llm.SYSTEM_PROMPT


def test_review_form_has_no_state_for_an_airport():
    from app.review.routes import _fields
    paths = {f["path"] for f in _fields({"item_type": "flight", "flight": {}})}
    assert not any(p.endswith("/region") for p in paths)
