"""Photon geocoding guardrail tests — offline, with a stubbed Photon.

The behaviour under test is the REFUSAL logic, which is what protects real
data: measured during bring-up, a bare chain-hotel name geocoded to a
different branch of the same chain 1500km away, and "Glendale, US" resolves
to California when the booking was in Arizona.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from resolve.geocode import GeocodeResult, PhotonGeocoder, apply_geocode


class _StubPhoton(PhotonGeocoder):
    """Returns canned features per query substring; records what was asked."""

    def __init__(self, responses):
        super().__init__("http://stub")
        self._responses = responses
        self.queries = []

    def _query(self, q, limit=5):
        self.queries.append(q)
        for needle, feats in self._responses.items():
            if needle.lower() in q.lower():
                return feats
        return []


def _feature(lat, lon, *, name=None, city=None, cc=None):
    return {"geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"name": name, "city": city, "countrycode": cc}}


def test_rejects_wrong_country():
    """The Humble-Texas case: right chain, wrong place."""
    g = _StubPhoton({"SureStay": [_feature(29.9, -95.3, name="SureStay Humble",
                                           city="Humble", cc="US")]})
    place = {"raw": "SureStay Madrid", "name": "SureStay Madrid",
             "city": "Madrid", "country": "ES"}
    assert g.geocode_place(place) is None


def test_rejects_wrong_city_in_same_country():
    g = _StubPhoton({"Glendale": [_feature(33.15, -117.35, name="Glendale",
                                           city="Glendale", cc="US")]})
    place = {"raw": "Hotel", "name": "Hotel", "city": "Mesa", "country": "US"}
    assert g.geocode_place(place) is None


def test_accepts_consistent_area_hit():
    g = _StubPhoton({"Casa Grande": [_feature(32.9, -105.96, name="Casa Grande",
                                             city="Casa Grande", cc="US")]})
    place = {"raw": "SureStay", "name": "SureStay", "city": "Casa Grande",
             "region": "Arizona", "country": "US"}
    r = g.geocode_place(place)
    assert r is not None and r.confidence == "verified"
    assert r.country == "US"


def test_folds_diacritics_when_comparing():
    """Document says "Zurich"; OSM says "Zürich". Must still match."""
    g = _StubPhoton({"Zurich": [_feature(47.37, 8.54, name="Zürich",
                                         city="Zürich", cc="CH")]})
    r = g.geocode_place({"raw": "Limmat House", "name": "Limmat House", "city": "Zurich",
                         "country": "CH"})
    assert r is not None and r.country == "CH"


def test_no_context_means_no_area_query_at_all():
    """Without a stated city there is nothing to verify against, so the
    geocoder must not fall back to a bare-name guess."""
    g = _StubPhoton({"Mystery": [_feature(0.0, 0.0, name="Mystery", cc="XX")]})
    assert g.geocode_place({"raw": "Mystery Hotel", "name": "Mystery Hotel"}) is None
    assert all("mystery hotel" == q.lower() or "," in q for q in g.queries) or not g.queries


def test_address_query_is_tried_before_city():
    """Booking emails print the street address; using it is what turns a
    city-centroid guess into the actual building."""
    g = _StubPhoton({})
    g.geocode_place({"raw": "Hotel", "name": "Hotel", "address": "311 Ocotillo Ln",
                     "city": "Glendale", "region": "Arizona", "country": "US"})
    assert g.queries, "no query issued"
    assert g.queries[0].startswith("311 Ocotillo Ln"), g.queries[0]
    assert "Arizona" in g.queries[0]      # region disambiguates Glendale CA/NM


def test_street_level_hit_is_poi_city_hit_is_area():
    street = _StubPhoton({"Pompa": [{"geometry": {"coordinates": [-104.23, 32.41]},
                                     "properties": {"housenumber": "311",
                                                    "street": "Ocotillo Ln",
                                                    "city": "Glendale", "countrycode": "US"}}]})
    r = street.geocode_place({"raw": "H", "name": "H", "address": "311 Ocotillo Ln",
                              "city": "Glendale", "country": "US"})
    assert r is not None and r.precision == "poi"

    city = _StubPhoton({"Glendale": [_feature(32.4, -104.2, name="Glendale",
                                              city="Glendale", cc="US")]})
    r2 = city.geocode_place({"raw": "H", "name": "H", "city": "Glendale",
                             "country": "US"})
    assert r2 is not None and r2.precision == "area"


def _street_feature(lat, lon, *, name, housenumber, street, city, state, cc):
    return {"geometry": {"coordinates": [lon, lat]},
            "properties": {"name": name, "housenumber": housenumber,
                           "street": street, "city": city, "state": state,
                           "countrycode": cc}}


def test_formatted_address_geocodes_without_structured_fields():
    """A Google Maps pick / iCal LOCATION is the whole address in one string,
    with no structured city or country. A street-level hit for it is
    self-verifying, and fills city/region/country/address."""
    addr = "CAA Ed Lakeshore Playhouse Theatre, 244 Victoria St, Toronto, ON M5B 1V8, Canada"
    g = _StubPhoton({"Victoria": [_street_feature(
        43.655, -79.379, name="Ed Lakeshore Playhouse Theatre", housenumber="244",
        street="Victoria Street", city="Toronto", state="Ontario", cc="CA")]})
    r = g.geocode_place({"raw": addr, "name": addr})
    assert r is not None and r.confidence == "verified" and r.precision == "poi"
    assert r.city == "Toronto" and r.country == "CA"
    assert r.region == "Ontario" and r.address == "244 Victoria Street"
    out = apply_geocode({"raw": addr, "name": addr}, r)
    assert out["city"] == "Toronto" and out["country"] == "CA"
    assert out["region"] == "Ontario" and out["address"] == "244 Victoria Street"


def test_address_tier_trusts_a_building_not_a_centroid():
    """The address tier accepts a BUILDING only: a non-street hit from the
    whole-string query is not self-verifying and must be dropped."""
    addr = "Some Park, 1 Loop Rd, Toronto, Canada"
    g = _StubPhoton({"Loop": [_feature(43.65, -79.38, name="Toronto",
                                       city="Toronto", cc="CA")]})   # no housenumber
    r = g.geocode_place({"raw": addr, "name": addr})
    assert apply_geocode({"raw": addr}, r) == {"raw": addr}          # nothing filled


def test_bare_name_without_address_structure_is_not_queried():
    """A comma-less / digit-less name never triggers the address tier — the
    wrong-branch guard stays in force."""
    g = _StubPhoton({"Starbucks": [_street_feature(
        1.0, 2.0, name="Starbucks", housenumber="9", street="Random St",
        city="Nowhere", state="X", cc="ZZ")]})
    assert g.geocode_place({"raw": "Starbucks", "name": "Starbucks"}) is None
    assert g.queries == []                                            # nothing asked


def test_apply_geocode_never_overwrites_document_values():
    r = GeocodeResult(lat=1.0, lon=2.0, country="FR", city="Paris",
                      name="Paris", confidence="verified", precision="area")
    place = {"raw": "Somewhere", "lat": 48.85, "lon": 2.35, "country": "BE"}
    out = apply_geocode(place, r)
    assert out["lat"] == 48.85 and out["country"] == "BE"   # document wins
    assert "geocode_source" not in out                       # nothing was filled


def test_apply_geocode_fills_blanks_and_records_source():
    r = GeocodeResult(lat=1.0, lon=2.0, country="fr", city="Paris",
                      name="Paris", confidence="verified", precision="area")
    out = apply_geocode({"raw": "Somewhere"}, r)
    assert out["lat"] == 1.0 and out["country"] == "FR"
    assert out["geocode_source"] == "photon"


def test_unverified_results_are_discarded_by_default():
    r = GeocodeResult(lat=1.0, lon=2.0, country=None, city=None, name=None,
                      confidence="unverified", precision="area")
    assert apply_geocode({"raw": "X"}, r) == {"raw": "X"}


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as ex:
                failures += 1
                print(f"  FAIL {name}: {str(ex)[:300]}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
