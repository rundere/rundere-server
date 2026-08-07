"""Revision ladder tests (D-13). Synthetic payloads only."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from resolve.revision import (PriorItem, changed_pointers, content_hash, decide)

BASE = {"item_type": "lodging", "booking_status": "confirmed",
        "booking_reference": "ABC123",
        "lodging": {"property_name": "Test Hotel",
                    "check_in": {"local": "2026-09-15", "precision": "date"},
                    "check_out": {"local": "2026-09-18", "precision": "date"}}}


def test_hash_is_key_order_independent():
    a = {"x": 1, "y": {"a": 1, "b": 2}}
    b = {"y": {"b": 2, "a": 1}, "x": 1}
    assert content_hash(a) == content_hash(b)


def test_no_prior_is_create():
    d = decide(BASE, None)
    assert d.action == "create" and d.revision == 1


def test_identical_recapture_is_no_change():
    """The 7 duplicate debugging captures of one booking are exactly this."""
    prior = PriorItem(id="i1", content_hash=content_hash(BASE))
    d = decide(BASE, prior)
    assert d.action == "no_change"
    assert d.prior_item_id == "i1"


def test_real_change_is_update_with_pointers():
    changed = {**BASE, "lodging": {**BASE["lodging"],
                                   "check_out": {"local": "2026-09-19",
                                                 "precision": "date"}}}
    prior = PriorItem(id="i1", content_hash=content_hash(BASE))
    d = decide(changed, prior, prior_payload=BASE)
    assert d.action == "update" and d.revision == 2
    assert "/lodging/check_out/local" in d.changed_fields


def test_cancellation_supersedes():
    cancelled = {**BASE, "booking_status": "cancelled"}
    prior = PriorItem(id="i1", content_hash=content_hash(BASE))
    d = decide(cancelled, prior, prior_payload=BASE)
    assert d.action == "supersede"
    assert d.prior_item_id == "i1"


def test_stale_document_becomes_conflict_not_a_write():
    """Out-of-order delivery must not let an older document overwrite a newer
    one (RULE-14) — common with airline schedule-change mail."""
    changed = {**BASE, "booking_reference": "ZZZ999"}
    prior = PriorItem(id="i1", content_hash=content_hash(BASE),
                      document_timestamp="2026-08-02T10:00:00Z")
    d = decide(changed, prior, document_timestamp="2026-08-01T09:00:00Z",
               prior_payload=BASE)
    assert d.action == "conflict" and d.stale is True
    assert d.review_required is True                     # RULE-15b


def test_locked_field_collision_is_conflict():
    """A human corrected this field in the review UI; never overwrite (RULE-15)."""
    changed = {**BASE, "lodging": {**BASE["lodging"], "property_name": "Robot Name"}}
    prior = PriorItem(id="i1", content_hash=content_hash(BASE),
                      field_locks=("/lodging/property_name",))
    d = decide(changed, prior, prior_payload=BASE)
    assert d.action == "conflict" and d.review_required is True


def test_changed_pointers_handles_lists_and_missing():
    old = {"a": [1, 2], "b": "x"}
    new = {"a": [1, 3], "c": "y"}
    ptrs = changed_pointers(old, new)
    assert "/a/1" in ptrs and "/b" in ptrs and "/c" in ptrs


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
