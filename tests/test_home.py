"""Home matching + suppression tests (D-07). Generic fixtures — no real
addresses; the effective-dating scenario mirrors the shape of a real
multi-home history without encoding anyone's actual one."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from resolve.home import (HomeProfileRef, annotate_place, haversine_km,
                          match_place, profiles_in_force, within_home_radius)

# Two eras: airport AAA + city until 2020, airport BBB + city after.
OLD_AIR = HomeProfileRef(id="p1", kind="home_airport", label="Old (AAA)",
                         iata="AAA", lat=40.0, lon=-75.0,
                         effective_from=None, effective_to="2020-01-01")
OLD_CITY = HomeProfileRef(id="p2", kind="residence", label="Old City",
                          lat=40.0, lon=-75.0, radius_km=50.0,
                          effective_from=None, effective_to="2020-01-01")
NEW_AIR = HomeProfileRef(id="p3", kind="home_airport", label="New (BBB)",
                         iata="BBB", lat=43.7, lon=-79.6,
                         effective_from="2020-01-01", effective_to=None)
NEW_CITY = HomeProfileRef(id="p4", kind="residence", label="New City",
                          lat=43.65, lon=-79.38, radius_km=50.0,
                          effective_from="2020-01-01", effective_to=None)
ALL = [OLD_AIR, OLD_CITY, NEW_AIR, NEW_CITY]


def test_effective_dating_picks_the_home_in_force_then():
    """The whole point of D-07: a 2013 flight is judged against 2013's home."""
    old = {p.id for p in profiles_in_force(ALL, "2013-06-01")}
    assert old == {"p1", "p2"}
    new = {p.id for p in profiles_in_force(ALL, "2024-06-01")}
    assert new == {"p3", "p4"}


def test_no_date_uses_only_currently_open_profiles():
    assert {p.id for p in profiles_in_force(ALL, None)} == {"p3", "p4"}


def test_boundary_date_belongs_to_the_new_era():
    assert {p.id for p in profiles_in_force(ALL, "2020-01-01")} == {"p3", "p4"}


def test_airport_matches_only_in_its_era():
    hit, _ = match_place({"raw": "Old", "iata": "AAA"}, ALL, "2013-06-01")
    assert hit is not None and hit.id == "p1"
    miss, _ = match_place({"raw": "Old", "iata": "AAA"}, ALL, "2024-06-01")
    assert miss is None                       # AAA is just a destination now


def test_metro_code_matches_member_airport():
    """A booking system may emit the city code instead of the airport."""
    hit, _ = match_place({"raw": "Toronto", "iata": "YTO"},
                         [HomeProfileRef(id="m", kind="home_airport",
                                         label="YYZ", iata="YYZ")], None)
    assert hit is not None and hit.id == "m"
    # and the reverse direction
    hit2, _ = match_place({"raw": "Pearson", "iata": "YYZ"},
                          [HomeProfileRef(id="m2", kind="home_airport",
                                          label="Toronto", iata="YTO")], None)
    assert hit2 is not None


def test_radius_match_and_distance_reported_when_not_home():
    near = {"raw": "Nearby", "lat": 43.70, "lon": -79.42}
    hit, dist = match_place(near, ALL, "2024-06-01")
    assert hit is not None and dist is not None and dist < 50
    far = {"raw": "Ottawa-ish", "lat": 45.42, "lon": -75.70}
    miss, far_dist = match_place(far, ALL, "2024-06-01")
    assert miss is None and far_dist and far_dist > 300   # distance still measured


def test_annotate_suppresses_but_never_deletes():
    """D-07: suppression is a write decision, not deletion."""
    place = {"raw": "BBB Airport", "iata": "BBB", "name": "New Intl",
             "city": "New City"}
    out = annotate_place(place, ALL, "2024-06-01")
    assert out["is_home"] is True
    assert out["suppressed"] is True
    assert out["suppression_reason"] == "home"       # RULE-24b needs a reason
    assert out["home_ref"] == "p3"                   # RULE-24 needs the ref
    assert out["name"] == "New Intl" and out["city"] == "New City"  # kept
    assert place.get("is_home") is None              # input not mutated


def test_annotate_non_home_marks_false_without_suppressing():
    out = annotate_place({"raw": "Paris", "lat": 48.85, "lon": 2.35}, ALL,
                         "2024-06-01")
    assert out["is_home"] is False
    assert "suppressed" not in out
    assert out["distance_from_home_km"] > 5000


def test_within_home_radius_needs_all_places():
    home = {"is_home": True}
    away = {"is_home": False}
    assert within_home_radius([home, home]) is True
    assert within_home_radius([home, away]) is False
    assert within_home_radius([]) is False          # empty is not "within home"


def test_haversine_known_distance():
    # Toronto -> Ottawa is ~350km
    d = haversine_km(43.6532, -79.3832, 45.4215, -75.6972)
    assert 330 < d < 370


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
