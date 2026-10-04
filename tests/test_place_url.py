"""YONDER-200: Google / Apple Maps PLACE link parsing, routing and categories.
Pure and offline — short-link expansion is stubbed, never fetched."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extract import maps_url, place_url

G_PLACE = ("https://www.google.com/maps/place/Sagrada+Fam%C3%ADlia/@41.4036,2.1744,17z/"
           "data=!3m1!4b1!4m6!3m5!1s0x12a4a2:0x1!8m2!3d41.4036299!4d2.1743558!16zL20")
G_DIR = "https://www.google.com/maps/dir/Toronto,+ON/Ottawa,+ON/@44.7,-77.0,8z"
G_API = "https://www.google.com/maps/search/?api=1&query=Caf%C3%A9+Central&query_place_id=ChIJabc"
A_Q = "https://maps.apple.com/?q=Le+Bernardin&ll=40.7615,-73.9817&address=155+W+51st+St"
A_PLACE = ("https://maps.apple.com/place?name=Louvre&coordinate=48.8606,2.3376"
           "&address=Rue+de+Rivoli&place-id=I123")
A_DIR = "https://maps.apple.com/?saddr=Toronto&daddr=Ottawa&dirflg=d"
SHORT = "https://maps.app.goo.gl/AbCdEf"


def test_google_place_uses_the_pin_not_the_viewport():
    p = place_url.parse_place_url(G_PLACE)
    assert p["provider"] == "google" and p["name"] == "Sagrada Família"
    assert (p["lat"], p["lon"]) == (41.4036299, 2.1743558)      # !3d/!4d, not @
    assert p["source_url"] == G_PLACE                            # kept for the Map button


def test_google_place_without_data_blob_falls_back_to_viewport():
    p = place_url.parse_place_url("https://www.google.com/maps/place/Foo+Bar/@10.5,20.5,15z")
    assert p["name"] == "Foo Bar" and (p["lat"], p["lon"]) == (10.5, 20.5)


def test_google_api_query_and_place_id():
    p = place_url.parse_place_url(G_API)
    assert p["name"] == "Café Central" and p["place_id"] == "ChIJabc" and p["lat"] is None


def test_google_bare_coordinate_query_is_a_pin_without_a_name():
    p = place_url.parse_place_url("https://www.google.com/maps?q=41.40,2.17")
    assert p["name"] is None and (p["lat"], p["lon"]) == (41.40, 2.17)


def test_apple_place_forms():
    q = place_url.parse_place_url(A_Q)
    assert q["provider"] == "apple" and q["name"] == "Le Bernardin"
    assert (q["lat"], q["lon"]) == (40.7615, -73.9817) and q["address"] == "155 W 51st St"
    p = place_url.parse_place_url(A_PLACE)
    assert p["name"] == "Louvre" and (p["lat"], p["lon"]) == (48.8606, 2.3376)
    assert p["place_id"] == "I123"


def test_directions_and_junk_are_not_places():
    for u in (G_DIR, A_DIR, "https://example.com/maps/place/Foo", "not a url",
              "https://www.google.com/maps/dir/?api=1&origin=A&destination=B"):
        assert place_url.parse_place_url(u) is None, u


def test_apple_short_link_is_never_guessed_at():
    assert place_url.parse_place_url("https://maps.apple/p/AbC123") is None
    assert place_url.classify("https://maps.apple/p/AbC123") is None


def test_out_of_range_coordinates_are_dropped():
    p = place_url.parse_place_url("https://maps.apple.com/?q=Nowhere&ll=95.0,200.0")
    assert p["name"] == "Nowhere" and p["lat"] is None and p["lon"] is None


def test_classify_routes_place_vs_directions():
    assert place_url.classify(G_PLACE) == "place"
    assert place_url.classify(A_Q) == "place"
    assert place_url.classify(G_DIR) == "directions"
    assert place_url.classify(A_DIR) == "directions"
    assert place_url.classify("https://example.com/") is None


def test_short_link_classified_after_expansion(monkeypatch):
    monkeypatch.setattr(maps_url, "_expand_short", lambda u: G_PLACE)
    assert place_url.classify(SHORT) == "place"
    assert place_url.parse_place_url(place_url.resolved_url(SHORT))["name"] == "Sagrada Família"
    monkeypatch.setattr(maps_url, "_expand_short", lambda u: G_DIR)
    assert place_url.classify(SHORT) == "directions"


def test_short_link_never_touches_the_network_when_asked_not_to(monkeypatch):
    def boom(u):
        raise AssertionError("must not expand")
    monkeypatch.setattr(maps_url, "_expand_short", boom)
    assert place_url.classify(SHORT, expand=False) is None


def test_failed_expansion_is_not_a_place(monkeypatch):
    monkeypatch.setattr(maps_url, "_expand_short", lambda u: u)   # best-effort failure
    assert place_url.classify(SHORT) is None


def test_non_allowlisted_short_host_is_not_expanded():
    # _expand_short itself returns non-Google hosts untouched (D-152 SSRF guard).
    assert maps_url._expand_short("https://evil.example/maps/x") == "https://evil.example/maps/x"
    assert place_url.classify("https://evil.example/maps/x") is None


def test_category_mapping_covers_both_sources():
    assert place_url.category_for("MKPOICategoryRestaurant") == {
        "topic": "food", "activity": "dining", "kind": "restaurant"}
    assert place_url.category_for("amenity=pub")["kind"] == "pub"
    assert place_url.category_for("tourism=museum")["activity"] == "tour"
    hotel = place_url.category_for("MKPOICategoryHotel")
    assert hotel["activity"] == "lodging" and hotel["topic"] is None
    assert place_url.category_for("tourism=hotel") == hotel | {}


def test_unknown_or_missing_category_falls_back_to_activity():
    for c in (None, "", "MKPOICategoryParking", "shop=whatever"):
        assert place_url.category_for(c) == {"topic": "events", "kind": "activity",
                                             "activity": "tour"}


def test_is_maps_link_accepts_only_google_and_apple_maps_hosts():
    for ok in (G_PLACE, G_API, A_Q, A_PLACE, SHORT, "https://maps.google.com/?q=x",
               "https://www.google.co.uk/maps/place/Foo", "https://maps.apple/p/AbC123"):
        assert place_url.is_maps_link(ok), ok
    for bad in ("https://example.com/maps/place/Foo", "https://www.google.com/search?q=x",
                "https://evil.example/https://www.google.com/maps/place/Foo",
                "javascript:alert(1)", "ftp://maps.apple.com/?q=x", "not a url", "", None):
        assert not place_url.is_maps_link(bad), bad
