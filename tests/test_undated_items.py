"""D-210 (SPEC-8 §12; YONDER-317): a trip item may have no date yet.

An undated directions capture is extracted as a normal ground-transport item
with an explicit "no date yet" (precision none), resolves as approvable with a
non-blocking "no travel date yet" prompt, takes a date in the usual Inbox
editor as the person's own (RULE-34), and commits to Run·dere's timeline
undated while a date-bound external write waits. All places are invented."""
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

try:
    from app import committing as C
    from app.db import SessionLocal, engine
    from app.models import Base, ItemKey, Trip, TravelItem
    from app.resolving import resolve_pending
    from app.review import service
    Base.metadata.create_all(engine)
except ImportError:                                  # bare host: the container/CI runs these
    pytest.skip("app dependencies not installed", allow_module_level=True)

import validation
from extract import llm, maps_url
from resolve.keys import has_undated

NOW = "2031-01-05T12:00:00Z"
ROUTE = ("https://www.google.com/maps/dir/Cottonwood+Lodge/Ocotillo+Mesa/"
         "data=!4m2!4m1!3e0")


# ------------------------------------------------------------------ schema
def _batch(time):
    return {"declared_segment_count": None, "items": [{
        "item_type": "ground_transport", "booking_status": "planned", "title": "Drive",
        "ground_transport": {"mode": "car",
                             "departure": {"place": {"raw": "Cottonwood Lodge"}, "time": time},
                             "arrival": {"place": {"raw": "Ocotillo Mesa"}, "time": time},
                             "route": {"provider": "google_maps", "is_estimate": True}}}]}


def test_no_date_yet_is_valid_and_explicit():
    schema = llm.load_extraction_schema()
    assert validation.validate_schema(_batch({"precision": "none"}), schema) == []
    # any OTHER precision still needs its value: no silent undated "date"
    assert validation.validate_schema(_batch({"precision": "date"}), schema)


def test_the_llm_grammar_never_offers_no_date():
    """A document states its dates; "none" is the person's choice, never the
    model's, and an optional `local` is one the model would stop emitting (D-29)."""
    import json
    grammar = json.dumps(llm.build_format_schema(llm.load_extraction_schema()))
    assert '"none"' not in grammar and '"if"' not in grammar


# ------------------------------------------------------------ the full path
def _trip(s, name):
    t = Trip(name=name, source="manual", start_date="2031-05-12", end_date="2031-05-20")
    s.add(t)
    s.commit()
    return t


def _undated_capture(s, trip_id):
    [payload] = maps_url.extract_maps_url(ROUTE, {})
    # Stops located, as the resolver's geocoder would (invented coordinates).
    payload["ground_transport"]["departure"]["place"].update(lat=35.08, lon=-106.65)
    payload["ground_transport"]["arrival"]["place"].update(lat=35.40, lon=-106.10)
    it = TravelItem(batch_id=str(uuid.uuid4()), segment_index=0,
                    item_type="ground_transport", schema_version="1.4.0", trip_id=trip_id,
                    source={"type": "maps", "ref": "r", "received_at": NOW,
                            "url": ROUTE, "capture_context": {"travel_date": None}},
                    extraction={"extractor": "maps_url", "ran_at": NOW, "confidence": 1.0,
                                "payload": payload},
                    review={"trip_pinned": True},
                    status={"extract": "ok", "resolve": "pending", "airtrail": "skipped",
                            "adventurelog": {}})
    s.add(it)
    s.commit()
    return it


def test_an_undated_route_is_approvable_and_takes_a_date_as_the_persons_own():
    with SessionLocal() as s:
        trip = _trip(s, f"Desert loop {uuid.uuid4().hex[:6]}")
        it = _undated_capture(s, trip.id)
        assert has_undated(it.extraction["payload"])

        resolve_pending(s, adventurelog=None)
        done = s.get(TravelItem, it.id)
        assert done.status["resolve"] == "ok"                      # not blocked
        assert done.review["required"] is False
        assert done.review["reasons"] == ["missing_travel_date"]   # the Inbox's prompt
        assert done.canonical_key.startswith("road:undated:")

        changed = service.apply_edits(s, done, fields={
            "/ground_transport/departure/time": "2031-05-14 09:30"})
        assert changed == ["/ground_transport/departure/time"]
        done = s.get(TravelItem, it.id)
        g = done.extraction["payload"]["ground_transport"]
        assert g["departure"]["time"]["origin"] == "capture_context"   # theirs (RULE-34)
        assert g["arrival"]["time"] == {"local": "2031-05-14", "precision": "date",
                                        "origin": "capture_context"}   # same day, no clock
        cc = done.source["capture_context"]
        assert cc["travel_date"] == "2031-05-14" and cc["travel_time"] == "09:30"
        assert done.status["resolve"] == "ok" and done.review["reasons"] == []
        assert not has_undated(done.extraction["payload"])
        assert s.query(ItemKey).filter(ItemKey.item_id == done.id,
                                       ItemKey.key.like("road:2031-05-14:%")).first()


class _FakeAL:
    """Never called for an undated item: the write must wait for a date."""
    def __getattr__(self, name):
        raise AssertionError(f"AdventureLog was called ({name}) for an undated item")


def test_an_undated_item_commits_to_the_timeline_and_external_writes_wait():
    from app.models import TripActivity
    with SessionLocal() as s:
        trip = _trip(s, f"Desert loop {uuid.uuid4().hex[:6]}")
        it = _undated_capture(s, trip.id)
        resolve_pending(s, adventurelog=None)
        it = s.get(TravelItem, it.id)
        it.resolution = {**it.resolution,
                         "targets": ["yonder.activity", "adventurelog.transportation"]}
        it.review = {**it.review, "action_taken": "approved"}
        s.commit()
        env = C.commit_item(s, it, _FakeAL(), None, dry_run=False)
        rows = {r["target"]: r for r in env["results"]}
        assert rows["yonder.activity"]["ok"] and not rows["yonder.activity"].get("skipped")
        assert rows["adventurelog.transportation"]["skipped"] == C.NO_DATE_SKIP
        assert C.is_external_skip(rows["adventurelog.transportation"])  # a later commit retries
        act = s.query(TripActivity).filter_by(trip_id=trip.id).one()
        assert act.starts_at == ""                                       # "Date not set"


def test_a_route_on_a_rundere_trip_needs_no_adventurelog_collection():
    """RULE-32 (D-05) asked every directions capture for an AdventureLog
    collection, so a route filed to a Run·dere trip, dated or not, could never
    be approved without AdventureLog. Since D-146/D-192 AdventureLog is optional
    and second: the rule applies only to a route that is going there."""
    import rules
    item = {"extraction": {"extractor": "maps_url", "payload": {"item_type": "ground_transport"}},
            "source": {"capture_context": {"travel_date": "2031-05-14"}},
            "resolution": {"outcome": "resolved", "targets": ["yonder.activity"]}}
    assert "RULE-32" not in {v.rule_id for v in rules.check_item(item)}
    item["resolution"]["targets"].append("adventurelog.transportation")
    assert "RULE-32" in {v.rule_id for v in rules.check_item(item)}


def test_the_inbox_shows_an_undated_route_with_its_prompt_and_an_empty_date():
    from fastapi.testclient import TestClient
    from app.main import app
    with SessionLocal() as s:
        trip = _trip(s, f"Desert loop {uuid.uuid4().hex[:6]}")
        it = _undated_capture(s, trip.id)
        resolve_pending(s, adventurelog=None)
        item_id = it.id
    html = TestClient(app).get(f"/review/item/{item_id}").text
    assert "No travel date yet" in html and "approve it as it is" in html
    assert 'name="f:/ground_transport/departure/time"' in html        # the usual editor
