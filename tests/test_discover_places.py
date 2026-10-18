"""First-party location derivation tests (SPEC-3-discovery.md §1, D-62).

Fully offline: an injected airport resolver and a stub geocoder stand in for
AirTrail's airport DB and Photon, so the derivation logic — extraction per
activity type, verified-only geocoding, dedup + provenance, and the dropped
list — is exercised without any network.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from resolve.geocode import GeocodeResult
from discover.places import derive_locations, _looks_like_airport_code


def _act(activity_type, values, id="a1"):
    return SimpleNamespace(id=id, type=activity_type, values=values)


def _airport(country, municipality, lat=0.0, lon=0.0, icao=None):
    return SimpleNamespace(country=country, municipality=municipality,
                           lat=lat, lon=lon, icao=icao)


class _StubAirports:
    """(code) -> Airport|None from a canned dict, upper-cased."""
    def __init__(self, table):
        self.table = {k.upper(): v for k, v in table.items()}
        self.asked = []

    def __call__(self, code):
        self.asked.append(code)
        return self.table.get((code or "").strip().upper())


class _StubGeocoder:
    """geocode_place(place) -> GeocodeResult|None, matched on a substring of the
    place's name/city/address. `verified` toggles GeocodeResult.confidence."""
    def __init__(self, responses):
        self.responses = responses          # needle -> (lat, lon, country, city, verified)
        self.queries = []

    def geocode_place(self, place):
        blob = " ".join(str(place.get(k) or "") for k in
                        ("name", "raw", "city", "address")).lower()
        self.queries.append(blob)
        for needle, (lat, lon, country, city, verified) in self.responses.items():
            if needle.lower() in blob:
                return GeocodeResult(
                    lat=lat, lon=lon, country=country, city=city, name=city,
                    confidence="verified" if verified else "unverified")
        return None

    def geocode_freeform(self, text):
        """Top hit for a user-typed string — always 'verified' (human vouches),
        mirroring PhotonGeocoder.geocode_freeform."""
        for needle, (lat, lon, country, city, _v) in self.responses.items():
            if needle.lower() in (text or "").lower():
                return GeocodeResult(lat=lat, lon=lon, country=country, city=city,
                                     name=city, confidence="verified")
        return None


_NO_GEO = _StubGeocoder({})


def _no_airport(code):
    return None


# ------------------------------------------------------------------ airports

def test_flight_derives_from_airport_reference():
    acts = [_act("flight", {"from": "YYZ", "to": "GVA"})]
    resolver = _StubAirports({
        "YYZ": _airport("CA", "Toronto"),
        "GVA": _airport("CH", "Geneva"),
    })
    out = derive_locations(acts, resolve_airport=resolver, geocoder=_NO_GEO)
    labels = {p.label for p in out.places}
    assert labels == {"Toronto, CA", "Geneva, CH"}
    assert out.countries == ["CA", "CH"]
    assert not out.dropped


def test_never_flown_airport_resolves_via_injected_lookup():
    """The flown reference wouldn't have NBO; the injected resolver (its DB
    backing in prod) still resolves it — so a first trip to a new country is not
    silently dropped."""
    acts = [_act("flight", {"from": "YYZ", "to": "NBO"})]
    resolver = _StubAirports({
        "YYZ": _airport("CA", "Toronto"),
        "NBO": _airport("KE", "Nairobi"),
    })
    out = derive_locations(acts, resolve_airport=resolver, geocoder=_NO_GEO)
    assert "KE" in out.countries


def test_unresolvable_airport_code_is_dropped():
    acts = [_act("flight", {"from": "YYZ", "to": "ZZZ"})]
    resolver = _StubAirports({"YYZ": _airport("CA", "Toronto")})
    out = derive_locations(acts, resolve_airport=resolver, geocoder=_NO_GEO)
    assert out.countries == ["CA"]
    assert [d["raw"] for d in out.dropped] == ["ZZZ"]


# ------------------------------------------------------------------ geocoded

def test_lodging_geocodes_verified_only():
    acts = [_act("lodging", {"name": "Hotel Lac Bleu", "city": "Geneva"})]
    geo = _StubGeocoder({"geneva": (46.2, 6.1, "CH", "Geneva", True)})
    out = derive_locations(acts, resolve_airport=_no_airport, geocoder=geo)
    assert [p.label for p in out.places] == ["Geneva, CH"]


def test_unverified_geocode_is_dropped_not_guessed():
    """The wrong-branch guard: an unverified hit is refused, and the place lands
    in `dropped` rather than being pinned to a plausible-wrong location."""
    acts = [_act("lodging", {"name": "SureStay Madrid", "city": "Madrid"})]
    geo = _StubGeocoder({"madrid": (29.9, -95.3, "US", "Humble", False)})
    out = derive_locations(acts, resolve_airport=_no_airport, geocoder=geo)
    assert out.places == []
    assert out.dropped and out.dropped[0]["activity_type"] == "lodging"


def test_manual_lodging_freeform_fallback_geocodes_address():
    """A hand-entered lodging with a free-text address and NO structured city:
    strict geocode_place can't verify it, but the freeform fallback (the human
    typed the address) resolves it — the fix for manual trips."""
    acts = [_act("lodging", {"name": "Hotel Lac Bleu",
                             "address": "10 Rue du Rhône, Geneva"})]
    geo = _StubGeocoder({"rhône": (46.2, 6.14, "CH", "Geneva", False)})
    out = derive_locations(acts, resolve_airport=_no_airport, geocoder=geo)
    assert [p.label for p in out.places] == ["Geneva, CH"]


def test_manual_lodging_bare_name_still_drops():
    """No freeform for a bare property name (no address) — the wrong-branch trap
    stays closed."""
    acts = [_act("lodging", {"name": "SureStay"})]
    geo = _StubGeocoder({"surestay": (29.9, -95.3, "US", "Humble", False)})
    out = derive_locations(acts, resolve_airport=_no_airport, geocoder=geo)
    assert out.places == [] and out.dropped


# ------------------------------------------------------------------ dedup

def test_dedup_merges_provenance_across_activities():
    """A flight into Geneva and a hotel in Geneva are ONE derived place carrying
    both source activities."""
    acts = [
        _act("flight", {"from": "YYZ", "to": "GVA"}, id="f1"),
        _act("lodging", {"name": "Hotel Lac Bleu", "city": "Geneva"}, id="l1"),
    ]
    resolver = _StubAirports({"YYZ": _airport("CA", "Toronto"),
                              "GVA": _airport("CH", "Geneva")})
    geo = _StubGeocoder({"geneva": (46.2, 6.1, "CH", "Geneva", True)})
    out = derive_locations(acts, resolve_airport=resolver, geocoder=geo)
    geneva = next(p for p in out.places if p.country == "CH")
    fields = {(pr["activity_type"], pr["field"]) for pr in geneva.provenance}
    assert ("flight", "to") in fields and ("lodging", "lodging") in fields
    assert geneva.city_key == "CH:Geneva"


# ------------------------------------------------------------------ extraction

def test_note_and_empty_activities_yield_nothing():
    acts = [_act("note", {"title": "Remember passport"}),
            _act("flight", {})]
    out = derive_locations(acts, resolve_airport=_no_airport, geocoder=_NO_GEO)
    assert out.places == [] and out.dropped == []


def test_train_cruise_tour_are_geocoded_as_places():
    acts = [
        _act("train", {"from": "Zurich", "to": "Milano"}, id="t1"),
        _act("cruise", {"departurePort": "Venice", "arrivalPort": "Split"}, id="c1"),
        _act("tour", {"name": "Old Town Walk", "location": "Split"}, id="to1"),
    ]
    geo = _StubGeocoder({
        "zurich": (47.4, 8.5, "CH", "Zurich", True),
        "milano": (45.5, 9.2, "IT", "Milan", True),
        "venice": (45.4, 12.3, "IT", "Venice", True),
        "split": (43.5, 16.4, "HR", "Split", True),
    })
    out = derive_locations(acts, resolve_airport=_no_airport, geocoder=geo)
    assert set(out.countries) == {"CH", "IT", "HR"}
    # Split appears from both the cruise arrival and the tour → deduped to one.
    splits = [p for p in out.places if p.country == "HR"]
    assert len(splits) == 1
    assert len(splits[0].provenance) == 2


# ------------------------------------------------------------------ helpers

def test_airport_code_detection_ignores_title_case_cities():
    assert _looks_like_airport_code("YYZ")
    assert _looks_like_airport_code("EGLL")
    assert not _looks_like_airport_code("Oslo")     # 4 letters but a city name
    assert not _looks_like_airport_code("Bern")
    assert not _looks_like_airport_code("Los Angeles")
