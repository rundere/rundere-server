"""Home suppression tests (SPEC-3-discovery.md; discover/home_filter).

Covers the pure matcher and the scope-aware runner behaviour — the key case
being a DOMESTIC leg: a non-home city in the home country still gets city-scope
topics (weather) while country-scope topics (plugs) are skipped.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from discover.home_filter import home_country_set, is_home_place
from discover.places import DerivedPlace
from resolve.home import HomeProfileRef

TORONTO = HomeProfileRef(id="1", kind="residence", label="Toronto",
                         lat=43.65, lon=-79.38, radius_km=50.0, country="CA")
ON = "2026-09-01"

_TO = DerivedPlace("CA", "Toronto", 43.68, -79.63)      # airport, ~21km from home
_GVA = DerivedPlace("CH", "Geneva", 46.23, 6.11)
_YVR = DerivedPlace("CA", "Vancouver", 49.19, -123.18)  # home country, not home


# ------------------------------------------------------------------ pure

def test_is_home_place_matches_within_radius():
    assert is_home_place(_TO, [TORONTO], ON)
    assert not is_home_place(_GVA, [TORONTO], ON)
    assert not is_home_place(_YVR, [TORONTO], ON)


def test_is_home_place_needs_coords():
    assert not is_home_place(DerivedPlace("CA", "Toronto", None, None), [TORONTO], ON)


def test_home_country_set_from_profile_and_match():
    assert home_country_set([_TO, _GVA], [TORONTO], ON) == {"CA"}


def test_home_country_set_infers_country_when_profile_omits_it():
    """A residence profile without a country still makes its country home via the
    coordinate match."""
    p = HomeProfileRef(id="1", kind="residence", label="Home",
                       lat=43.65, lon=-79.38, radius_km=50.0, country=None)
    assert home_country_set([_TO, _GVA], [p], ON) == {"CA"}


# ------------------------------------------------------------------ runner

try:
    from app.db import SessionLocal, engine
    from app.models import (Base, DiscoveryFinding, DiscoveryRun, Trip,
                            TripActivity)
    from discover.resolvers import PlugsResolver
    from discover.resolvers.base import Finding
    from discover.runner import run_discovery
    Base.metadata.create_all(engine)
    DEPS = True
except ImportError:
    DEPS = False

_COORDS = {"YYZ": ("CA", "Toronto", 43.68, -79.63),
           "GVA": ("CH", "Geneva", 46.23, 6.11),
           "YVR": ("CA", "Vancouver", 49.19, -123.18)}


def _airports(code):
    h = _COORDS.get((code or "").upper())
    return None if h is None else SimpleNamespace(
        country=h[0], municipality=h[1], lat=h[2], lon=h[3], icao=None)


class _NoGeo:
    def geocode_place(self, p):
        return None


class _FakeCity:
    topic = "weather"
    scope = "city"

    def resolve(self, place, ctx):
        return Finding(topic="weather", scope="city",
                       place_key=place.city_key or place.country,
                       body_md="w", resolver="fake")


def _trip_with(frm, to):
    with SessionLocal() as s:
        for m in (DiscoveryFinding, DiscoveryRun, TripActivity, Trip):
            s.query(m).delete()
        s.commit()
        t = Trip(name="t", start_date="2026-09-01", end_date="2026-09-10")
        s.add(t); s.commit()
        s.add(TripActivity(trip_id=t.id, type="flight", starts_at="2026-09-01",
                           values={"from": frm, "to": to}))
        s.commit()
        return t.id


def _run(tid):
    with SessionLocal() as s:
        run = run_discovery(s, s.get(Trip, tid), resolve_airport=_airports,
                            geocoder=_NoGeo(),
                            resolvers=[PlugsResolver(), _FakeCity()],
                            home_profiles=[TORONTO], today="2026-08-14")
        return run.id


def test_international_trip_suppresses_home_only():
    if not DEPS:
        return
    tid = _trip_with("YYZ", "GVA")
    rid = _run(tid)
    with SessionLocal() as s:
        fs = s.query(DiscoveryFinding).filter_by(trip_id=tid).all()
        got = {(f.topic, f.place_key) for f in fs}
        # CA plugs skipped (home country); Toronto weather skipped (home city).
        assert ("plugs", "CA") not in got
        assert ("weather", "CA:Toronto") not in got
        # Geneva fully researched.
        assert ("plugs", "CH") in got
        assert ("weather", "CH:Geneva") in got
        run = s.get(DiscoveryRun, rid)
        assert run.params["home_skipped"]["home_countries"] == ["CA"]
        assert run.params["home_skipped"]["home_cities"] == ["Toronto, CA"]


def test_domestic_leg_keeps_city_topic_drops_country_topic():
    if not DEPS:
        return
    tid = _trip_with("YYZ", "YVR")
    _run(tid)
    with SessionLocal() as s:
        got = {(f.topic, f.place_key)
               for f in s.query(DiscoveryFinding).filter_by(trip_id=tid).all()}
        # Both cities are in the home country → NO country-scope (plugs) at all.
        assert not any(topic == "plugs" for topic, _ in got)
        # Vancouver is not home → its weather still runs; Toronto (home) does not.
        assert ("weather", "CA:Vancouver") in got
        assert ("weather", "CA:Toronto") not in got
