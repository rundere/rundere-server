"""Traveller identity matching (D-22/RULE-10). Generic names only.

The consequential case is the negative one: a flight with no self aboard must
NOT reach AirTrail, because AirTrail holds the account holder's own history.
Until this existed, `self_on_booking` was hardcoded True and RULE-10 could
never fire.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from resolve.identity import (Identity, match_identity, name_tokens,
                              normalize_name, resolve_travelers)

SELF = Identity(id="1", display_name="Alex Vardhan", kind="self",
                aliases=("VARDHAN/ALEX MR",), airtrail_user_id="u-1")
GUEST = Identity(id="2", display_name="Priya Vardhan", kind="guest")
OTHER = Identity(id="3", display_name="Sam Okafor", kind="guest")
ALL = [SELF, GUEST, OTHER]


def test_normalization_strips_titles_punctuation_and_case():
    assert normalize_name("VARDHAN/ALEX MR") == "vardhan alex"
    assert normalize_name("Alex  Vardhan") == "alex vardhan"
    assert normalize_name("Ålex Vardhan") == "alex vardhan"


def test_word_order_does_not_decide_identity():
    """Airlines print LAST/FIRST; humans write First Last."""
    ident, source, _ = match_identity("VARDHAN/ALEX", ALL)
    assert ident is SELF and source in ("alias_exact", "alias_fuzzy")


def test_middle_name_still_matches():
    ident, _, _ = match_identity("Alex Kumar Vardhan", ALL)
    assert ident is SELF


def test_similar_family_member_is_not_confused_with_self():
    """THE dangerous false positive: a shared surname must not make a
    relative's ticket look like yours."""
    ident, _, _ = match_identity("Priya Vardhan", ALL)
    assert ident is GUEST
    ident2, _, _ = match_identity("PRIYA VARDHAN MRS", ALL)
    assert ident2 is GUEST


def test_single_initial_does_not_match_everyone():
    assert "a" not in name_tokens("A Vardhan")
    ident, _, _ = match_identity("A Vardhan", ALL)
    assert ident is not GUEST          # must not silently pick a relative


def test_unknown_person_is_unknown_not_self():
    ident, _, conf = match_identity("Jordan Nakamura", ALL)
    assert ident is None and conf == 0.0


def test_guest_only_booking_excludes_airtrail():
    """RULE-10's whole purpose: a flight the account holder was not on is not
    their flight history."""
    payload = {"item_type": "flight",
               "travelers": [{"raw_name": "Priya Vardhan"},
                             {"raw_name": "Sam Okafor"}]}
    tr = resolve_travelers(payload, ALL)
    assert tr.self_on_booking is False
    assert [t["role"] for t in tr.travelers] == ["guest", "guest"]


def test_self_plus_guests_includes_airtrail():
    payload = {"item_type": "flight",
               "travelers": [{"raw_name": "VARDHAN/ALEX MR"},
                             {"raw_name": "Priya Vardhan"}]}
    tr = resolve_travelers(payload, ALL)
    assert tr.self_on_booking is True
    assert [t["role"] for t in tr.travelers] == ["self", "guest"]


def test_absent_passenger_list_assumes_self_and_flags_it():
    """D-22 policy: assume self AND notify. RULE-11 checks the notify half."""
    tr = resolve_travelers({"item_type": "flight"}, ALL)
    assert tr.self_on_booking is True and tr.inferred is True
    assert tr.travelers[0]["match_source"] == "inferred_absent"


def test_targets_exclude_airtrail_for_guest_only():
    from resolve.targets import compute_targets
    targets = compute_targets({"item_type": "flight"}, self_on_booking=False,
                              within_home=False, has_collection=True,
                              unsuppressed_places=1, creating_collection=False)
    assert "airtrail.flight" not in targets
    assert "adventurelog.transportation" in targets   # the trip still happened


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
