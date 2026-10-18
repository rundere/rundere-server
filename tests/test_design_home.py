"""Home layout, sidebar badge, trip-card packing bar, 32px floor (D-165, YONDER-202)."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TODAY = date.today()


def _fixture():
    from fastapi.testclient import TestClient

    from app.db import engine
    from app.main import app
    from app.models import Base
    Base.metadata.create_all(engine)
    return TestClient(app)


def test_home_has_greeting_search_queue_and_trips_not_old_tiles():
    html = _fixture().get("/").text
    assert "Good " in html and 'role="search"' in html and 'name="q"' in html
    assert "Inbox" in html and "Upcoming trips" in html      # D-200: the queue panel is the Inbox
    # one Beacon primary on Home: Capture
    assert html.count('class="act primary"') == 1
    for gone in ("Review pipeline", "Capture activity", "Go to", "dash-stage", "dash-card"):
        assert gone not in html


def test_greeting_by_hour_and_name():
    from datetime import datetime

    from app.dashboard import _greeting
    assert _greeting("Ada", datetime(2026, 1, 1, 9)) == "Good morning, Ada"
    assert _greeting("Ada", datetime(2026, 1, 1, 14)) == "Good afternoon, Ada"
    assert _greeting("", datetime(2026, 1, 1, 21)) == "Good evening"


def test_greeting_uses_the_traveller_name_and_the_viewers_clock():
    """YONDER-218: "Kenji", not "admin"; and the page re-reads the hour from the
    browser, because the server may run on UTC."""
    import uuid
    from types import SimpleNamespace

    from app.dashboard import greeting_name
    from app.db import SessionLocal
    from app.models import TravelerIdentity
    with SessionLocal() as s:
        t = TravelerIdentity(display_name=f"Kenji Nakamura{uuid.uuid4().hex[:4]}", kind="guest")
        s.add(t)
        s.flush()
        assert greeting_name(s, SimpleNamespace(username="admin", traveler_id=t.id)) == "Kenji"
        assert greeting_name(s, SimpleNamespace(username="admin", traveler_id=None)) == "admin"
        assert greeting_name(s, None) == ""
        s.rollback()
    html = _fixture().get("/").text
    assert 'id="dash-greeting"' in html and "new Date().getHours()" in html


def test_home_trip_card_shows_packing_progress_per_user():
    from app.db import SessionLocal
    from app.models import PackingItem, Trip
    c = _fixture()
    with SessionLocal() as s:
        t = Trip(name="Home pack trip", source="manual", start_date=(TODAY + timedelta(days=5)).isoformat(),
                 end_date=(TODAY + timedelta(days=8)).isoformat())
        s.add(t); s.commit(); tid = t.id
        rows = [PackingItem(trip_id=tid, user_id=None, item_key=f"k{i}", label=f"i{i}",
                            category="clothing", status=st)
                for i, st in enumerate(("packed", "draft", "draft", "removed", "omitted"))]
        s.add_all(rows); s.commit()
    try:
        html = c.get("/").text
        assert "Home pack trip" in html and "1/3" in html and 'style="width:33%"' in html
        assert "1/3" in c.get("/trips/list").text
    finally:
        with SessionLocal() as s:
            s.query(PackingItem).filter(PackingItem.trip_id == tid).delete()
            s.query(Trip).filter(Trip.id == tid).delete()
            s.commit()


def test_packing_progress_is_scoped_to_the_viewer():
    _fixture()
    from types import SimpleNamespace

    from app.db import SessionLocal
    from app.models import PackingItem
    from app.trips.routes import packing_progress
    with SessionLocal() as s:
        s.add_all([PackingItem(trip_id="t-scope", user_id="u-a", item_key="a", label="a", category="x", status="packed"),
                   PackingItem(trip_id="t-scope", user_id="u-b", item_key="b", label="b", category="x", status="draft")])
        s.commit()
        try:
            assert packing_progress(s, ["t-scope"], SimpleNamespace(id="u-a"))["t-scope"]["total"] == 1
            assert packing_progress(s, ["t-scope"], SimpleNamespace(id="u-c")) == {}
        finally:
            s.query(PackingItem).filter(PackingItem.trip_id == "t-scope").delete()
            s.commit()


def test_review_badge_counts_attention_and_is_zero_on_failure(monkeypatch):
    from types import SimpleNamespace

    from app import icons
    req = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr("app.review.routes._all_views",
                        lambda s, u=None: [{"bucket": "attention"}, {"bucket": "ready"}, {"bucket": "attention"}])
    assert icons.review_badge(req) == 2
    req2 = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr("app.review.routes._all_views", lambda s, u=None: 1 / 0)
    assert icons.review_badge(req2) == 0


def test_capsule_has_dot_chips_are_filled_and_controls_floor_at_32():
    base = (ROOT / "app/review/templates/base.html").read_text()
    assert ".pill::before" in base and "width:6px;height:6px" in base
    css = (ROOT / "app/review/static/theme.css").read_text()
    assert "select,summary,.yn-tab{min-height:var(--rd-control-sm)}" in css
    trips = (ROOT / "app/trips/static/trips.css").read_text()
    assert ".tag-chip" in trips
    assert "D-165" in (ROOT / "SPEC-8-first-party.md").read_text()
