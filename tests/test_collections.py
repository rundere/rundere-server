"""A6 ladder tests: HANDOFF Task 3 asks for overlapping ranges, adjacent
trips, and the deferred path. Runnable under pytest or directly:
python3 tests/test_collections.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resolve.collections import (CollectionRef, collection_name,
                                 escalate_deferred, majority_month,
                                 primary_destination, recompute_name,
                                 resolve_collection)

UK = CollectionRef("c-uk", "United Kingdom — September 2026",
                   "2026-09-14", "2026-09-22")
FR = CollectionRef("c-fr", "France — September 2026",
                   "2026-09-18", "2026-09-28")


# ---------------------------------------------------------------- naming (D-23)

def test_majority_month_owner_example():
    """Aug 29 -> Sep 15 is a September trip, NOT August."""
    assert majority_month("2026-08-29", "2026-09-15") == (2026, 9)
    assert collection_name("United Kingdom", "2026-08-29", "2026-09-15") == \
        "United Kingdom — September 2026"


def test_majority_month_edges():
    assert majority_month("2026-09-14", "2026-09-14") == (2026, 9)   # single day
    assert majority_month("2026-12-28", "2027-01-10") == (2027, 1)   # year rollover
    assert collection_name("Japan", "2026-12-28", "2027-01-10") == \
        "Japan — January 2027"


def test_primary_destination_is_turnaround_not_final_leg():
    """YYZ->LHR (8-day stay) ->YYZ: the trip is to the UK, not Canada."""
    legs = [
        {"arrival_country": "United Kingdom",
         "arrival_time": "2026-09-14T06:40:00",
         "next_departure_time": "2026-09-22T11:00:00"},
        {"arrival_country": "Canada",
         "arrival_time": "2026-09-22T14:05:00",
         "next_departure_time": None},
    ]
    assert primary_destination(legs) == "United Kingdom"
    assert primary_destination([legs[0]]) == "United Kingdom"   # outbound only
    assert primary_destination([]) is None


def test_recompute_name_respects_human_rename():
    minted = CollectionRef("c1", "United Kingdom — August 2026",
                           "2026-08-29", "2026-08-29")
    # return flight arrives, span extends -> September now holds the majority
    assert recompute_name(minted, "United Kingdom",
                          "2026-08-29", "2026-09-15") == \
        "United Kingdom — September 2026"
    renamed = CollectionRef("c1", "Grandma's 80th", "2026-08-29", "2026-08-29",
                            human_renamed=True)
    assert recompute_name(renamed, "United Kingdom",
                          "2026-08-29", "2026-09-15") is None


# ---------------------------------------------------------------- the ladder

def test_user_selection_short_circuits_even_over_exact_match():
    """D-05 / RULE-35b: the resolver never overrides an explicit human choice."""
    r = resolve_collection("lodging", "2026-09-15", "2026-09-18", [UK],
                           capture_collection_id="c-other")
    assert r.collection_action == "user_selected"
    assert r.collection_id == "c-other"
    assert r.outcome == "resolved"


def test_exact_single_match():
    r = resolve_collection("lodging", "2026-09-15", "2026-09-18", [UK, FR])
    assert r.collection_action == "matched_exact"
    assert r.collection_id == "c-uk"          # only UK contains 15-18 fully
    assert r.match_attempted


def test_overlapping_ranges_go_to_review_never_guess():
    """Item span sits inside BOTH overlapping trips -> needs_review + candidates."""
    r = resolve_collection("lodging", "2026-09-19", "2026-09-21", [UK, FR])
    assert r.outcome == "needs_review"
    assert set(r.candidate_collections) == {"c-uk", "c-fr"}
    assert r.collection_action == "none"      # RULE-30: no mint with candidates


def test_adjacent_trip_never_fuzzy_matched():
    """OQ-14 (owner): a hotel 2 days before the trip is NOT auto-filed."""
    r = resolve_collection("lodging", "2026-09-12", "2026-09-13", [UK])
    assert r.collection_action == "none"      # not matched into c-uk
    assert r.outcome == "deferred"


def test_no_match_flight_mints_with_name_and_match_attempted():
    """D-04 + RULE-30b + D-23 naming."""
    r = resolve_collection("flight", "2026-08-29", "2026-09-15", [UK, FR],
                           mint_country="Portugal")
    assert r.collection_action == "created"
    assert r.match_attempted                  # RULE-30b: mint only after trying
    assert r.collection_name == "Portugal — September 2026"


def test_mint_without_destination_country_goes_to_review():
    r = resolve_collection("flight", "2026-08-29", "2026-09-15", [])
    assert r.outcome == "needs_review"
    assert r.collection_action == "none"


def test_deferred_path_and_two_day_escalation():
    """OQ-05 (owner): 2-day fuse, then the sweep escalates to review."""
    r = resolve_collection("lodging", "2026-10-01", "2026-10-05", [UK],
                           today="2026-08-03")
    assert r.outcome == "deferred"
    assert r.deferred_until == "2026-08-05"
    assert escalate_deferred(r, "2026-08-04").outcome == "deferred"    # waiting
    esc = escalate_deferred(r, "2026-08-06")
    assert esc.outcome == "needs_review"                               # escalated
    assert escalate_deferred(esc, "2026-08-07") == esc                 # idempotent


def test_event_and_ground_defer_like_lodging():
    for t in ("event", "ground_transport", "note"):
        r = resolve_collection(t, "2026-10-01", "2026-10-01", [])
        assert r.outcome == "deferred", t


# ---------------------------------------------------------------- runner

if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as ex:
                failures += 1
                print(f"  FAIL {name}: {ex}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
