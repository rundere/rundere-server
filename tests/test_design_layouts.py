"""YONDER-202 step 4: sidebar shell, Next-up card, trip cards."""
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False


def _flight(frm, to, when, num="BA 1"):
    return NS(type="flight", starts_at=when, values={"from": frm, "to": to, "flight_number": num})


def test_route_codes_are_ordered_unique_and_only_real_codes():
    from app.trips.routes import trip_route_codes
    acts = [_flight("NRT", "KIX", "2026-10-10T09:00"), _flight("LHR", "NRT", "2026-10-08T11:00"),
            _flight("Heathrow", "LHR", "2026-10-07T08:00"), NS(type="lodging", starts_at="", values={})]
    assert trip_route_codes(acts) == ["LHR", "NRT", "KIX"]
    assert trip_route_codes([], 4) == []


def test_trip_when_wording():
    from app.trips.routes import trip_when
    t = lambda s, e: NS(start_date=s, end_date=e)          # noqa: E731
    assert trip_when(t("2026-10-01", "2026-10-09"), "2026-10-05") == "In progress"
    assert trip_when(t("2026-10-12", "2026-10-15"), "2026-10-10") == "in 2 days"
    assert trip_when(t("2026-10-11", "2026-10-15"), "2026-10-10") == "in 1 day"
    assert trip_when(t("2026-09-01", "2026-09-04"), "2026-10-10") == "ended 36 days ago"


if DEPS:
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, Trip, TripActivity

    Base.metadata.create_all(engine)
    client = TestClient(app)
    TODAY = date.today()

    def _mk(name, start, end, flights=()):
        with SessionLocal() as s:
            t = Trip(name=name, source="manual", start_date=start.isoformat(), end_date=end.isoformat())
            s.add(t); s.flush()
            for frm, to, d, num in flights:
                s.add(TripActivity(trip_id=t.id, type="flight", starts_at=d.isoformat() + "T09:00",
                                   values={"from": frm, "to": to, "flight_number": num}))
            s.commit()
            return t.id

    def _clean(*ids):
        with SessionLocal() as s:
            s.query(TripActivity).filter(TripActivity.trip_id.in_(ids)).delete(synchronize_session=False)
            s.query(Trip).filter(Trip.id.in_(ids)).delete(synchronize_session=False)
            s.commit()

    def test_home_next_up_card_shows_the_soonest_flight_not_past_ones():
        soon = TODAY + timedelta(days=4)
        a = _mk("Layout A", TODAY - timedelta(days=3), TODAY + timedelta(days=9),
                [("AAA", "BBB", TODAY - timedelta(days=2), "OLD 1"), ("NRT", "KIX", soon, "NH 21")])
        b = _mk("Layout B", TODAY + timedelta(days=30), TODAY + timedelta(days=33),
                [("LGW", "LIS", TODAY + timedelta(days=30), "BA 502")])
        try:
            html = client.get("/").text
            assert 'class="nextup"' in html and "NEXT UP · FLIGHT · IN 4 DAYS" in html
            assert ">NRT<" in html and ">KIX<" in html and "NH 21" in html
            assert "OLD 1" not in html and ">AAA<" not in html and ">LGW<" not in html
        finally:
            _clean(a, b)

    def test_home_next_up_falls_back_to_the_trip_without_flights():
        t = _mk("Layout Nofly", TODAY + timedelta(days=12), TODAY + timedelta(days=15))
        try:
            from app.dashboard import _next_up
            with SessionLocal() as s:
                nu = _next_up(s, s.query(Trip).filter(Trip.id == t).all(), TODAY.isoformat())
            assert nu["kind"] == "trip" and nu["overline"] == "NEXT UP · TRIP · IN 12 DAYS"
        finally:
            _clean(t)

    def test_trips_list_renders_cards_with_codes_and_no_template_leak():
        t = _mk("Layout Card", TODAY + timedelta(days=20), TODAY + timedelta(days=24),
                [("LHR", "NRT", TODAY + timedelta(days=20), "BA 7")])
        try:
            html = client.get("/trips/list").text
            assert 'class="yn-tcard"' in html and "LHR · NRT" in html and "in 20 days" in html
            assert "1 item" in html and "built-in method" not in html and "<table" not in html
            assert f'href="/trips/{t}"' in html
        finally:
            _clean(t)

    def test_shell_markup_has_icons_in_the_nav_and_a_sidebar_ruleset():
        html = client.get("/").text
        assert html.count('class="ni"') >= 6
        css = (ROOT / "app/review/static/theme.css").read_text()
        assert "min-width:1024px" in css and "padding-left:248px" in css and "width:248px" in css
