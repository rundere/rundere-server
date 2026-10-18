"""YONDER-177 / D-172 — Next up is the soonest MOMENT ahead (a start or an end), so a
multi-day booking never holds the card for its whole span; what's under way is listed
as in progress. The reported case: a rental picked up Fri 20:00 and kept to Sunday must
not hide the hotel check-in at 22:00 or Saturday's 09:00 departure."""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.trips.nextup import label, plan, upcoming  # noqa: E402

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False


def _a(type_, when, end=None, aid=None, **values):
    v = dict(values, when=when)
    if end:
        v["endWhen"] = end
    return NS(id=aid or f"{type_}-{when}", type=type_, starts_at=when, values=v, trip_id="t")


# The reported weekend: Fri 2031-10-10, Sat 11th, Sun 12th.
RENTAL = _a("carRental", "2031-10-10T20:00", "2031-10-12T18:00", company="Hertz")
HOTEL = _a("lodging", "2031-10-10T22:00", "2031-10-11T09:00", name="Hotel Mesa Alta")
DRIVE = _a("drive", "2031-10-11T09:00", "2031-10-11T13:00", route="PHX → Sedona")
WEEKEND = [RENTAL, HOTEL, DRIVE]


def _next(acts, now):
    m, _ = plan(acts, now)
    return (m.activity.id, m.edge) if m else None


def _now(acts, now):
    return [m.activity.id for m in plan(acts, now)[1]]


def test_the_reported_rental_and_hotel_weekend():
    # Before pick-up: the rental is next.
    assert _next(WEEKEND, "2031-10-10T18:00") == (RENTAL.id, "start")
    assert _now(WEEKEND, "2031-10-10T18:00") == []
    # Rental picked up: the hotel check-in is next, the rental is in progress.
    assert _next(WEEKEND, "2031-10-10T20:30") == (HOTEL.id, "start")
    assert _now(WEEKEND, "2031-10-10T20:30") == [RENTAL.id]
    # Checked in: Saturday's check-out is next (an end ties before the 09:00 drive).
    assert _next(WEEKEND, "2031-10-10T23:00") == (HOTEL.id, "end")
    assert _now(WEEKEND, "2031-10-10T23:00") == [RENTAL.id]      # the hotel is on the card
    # On the road Saturday: the drive's arrival, then only the drop-off is left.
    assert _next(WEEKEND, "2031-10-11T10:00") == (DRIVE.id, "end")
    assert _now(WEEKEND, "2031-10-11T10:00") == [RENTAL.id]
    assert _next(WEEKEND, "2031-10-11T14:00") == (RENTAL.id, "end")
    assert _now(WEEKEND, "2031-10-11T14:00") == []               # the rental is on the card
    # All done.
    assert _next(WEEKEND, "2031-10-12T19:00") is None
    assert _now(WEEKEND, "2031-10-12T19:00") == []


def test_boundaries_are_exact():
    # At the start minute it has started; at the end minute it has ended.
    assert _now([HOTEL], "2031-10-10T22:00") == []               # card: its check-out
    assert _next([HOTEL], "2031-10-10T22:00") == (HOTEL.id, "end")
    assert _next([HOTEL], "2031-10-10T21:59") == (HOTEL.id, "start")
    assert _next([HOTEL], "2031-10-11T09:00") is None


def test_items_without_an_end_and_all_day_items():
    dinner = _a("dining", "2031-10-10T19:30", name="Mesa Diner")
    assert _next([dinner], "2031-10-10T19:00") == (dinner.id, "start")
    assert _next([dinner], "2031-10-10T19:30") is None           # a single moment: gone once it starts
    assert _now([dinner], "2031-10-10T19:45") == []
    day = _a("tour", "2031-10-11", name="Balloon Fiesta")       # date only, no end
    assert _next([day], "2031-10-10T23:00") == (day.id, "start")
    assert _now([day], "2031-10-11T15:00") == [day.id]           # under way all that day
    assert _now([day], "2031-10-12T00:00") == []
    # An end date with no time runs to the end of that day.
    stay = _a("lodging", "2031-10-10", "2031-10-12", name="Cabin")
    dinner2 = _a("dining", "2031-10-12T19:00")
    assert _now([stay, dinner2], "2031-10-11T20:00") == [stay.id]   # dinner is on the card
    assert _next([stay, dinner2], "2031-10-12T20:00") == (stay.id, "end")
    assert _next([stay, dinner2], "2031-10-13T00:00") is None


def test_notes_undated_and_backwards_items_are_handled():
    note = _a("note", "2031-10-10T08:00", title="Pack the chargers")
    undated = NS(id="u", type="tour", starts_at="", values={}, trip_id="t")
    backwards = _a("lodging", "2031-10-10T15:00", "2031-10-09T11:00", aid="bw")
    assert _next([note, undated], "2031-10-01T00:00") is None
    assert _next([backwards], "2031-10-01T00:00") == ("bw", "start")
    assert _next([backwards], "2031-10-10T16:00") is None        # a bad end is ignored


def test_zones_and_seconds_are_ignored():
    a = _a("flight", "2031-10-10T08:15:00+02:00", aid="z")
    m, _ = plan([a], "2031-10-10T08:00")
    assert m.at == "2031-10-10T08:15" and m.key == "2031-10-10T08:15"


def test_labels_name_the_moment():
    m, _ = plan([RENTAL], "2031-10-10T21:00")
    assert label(m) == "Drop-off"
    m, _ = plan([HOTEL], "2031-10-10T00:00")
    assert label(m) == "Check-in"
    m, _ = plan([_a("dining", "2031-10-10T19:30")], "2031-10-10T00:00")
    assert label(m) == "Restaurant"                               # not "Reservation time"


def _ahead(acts, now, window_h=48, limit=12):
    return [(m.activity.id, m.edge) for m in upcoming(acts, now, window_h=window_h, limit=limit)]


def test_upcoming_rolls_forward_through_the_weekend():
    # YONDER-250: every moment ahead in the window, soonest first; the 09:00 tie
    # keeps D-172's order (check out, then drive away).
    assert _ahead(WEEKEND, "2031-10-10T20:30") == [
        (HOTEL.id, "start"), (HOTEL.id, "end"), (DRIVE.id, "start"), (DRIVE.id, "end"),
        (RENTAL.id, "end")]
    # The first is always the Next-up card's moment.
    for now in ("2031-10-10T18:00", "2031-10-10T23:00", "2031-10-11T10:00"):
        m, _ = plan(WEEKEND, now)
        assert _ahead(WEEKEND, now)[0] == (m.activity.id, m.edge)
    assert _ahead(WEEKEND, "2031-10-12T19:00") == []


def test_upcoming_honours_the_window_and_the_limit():
    # 12 h from Fri 20:30 is Sat 08:30: only the check-in is inside it.
    assert _ahead(WEEKEND, "2031-10-10T20:30", window_h=12) == [(HOTEL.id, "start")]
    # The window's last minute is inside it (Sat 09:00 is 12 h 30 after 20:30).
    assert _ahead(WEEKEND, "2031-10-10T20:30", window_h=13)[:3] == [
        (HOTEL.id, "start"), (HOTEL.id, "end"), (DRIVE.id, "start")]
    assert _ahead(WEEKEND, "2031-10-10T20:30", limit=2) == [(HOTEL.id, "start"), (HOTEL.id, "end")]
    # A window crossing a month end.
    late = _a("flight", "2031-11-01T06:00", aid="nov")
    assert _ahead([late], "2031-10-31T20:00", window_h=12) == [("nov", "start")]


if DEPS:
    from app import authn
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, Trip, TripActivity, TripActivityTraveler, TripMember,
                            TripTraveler, User)

    Base.metadata.create_all(engine)
    client = TestClient(app)

    def _user(name, traveler_id=None):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                u = User(username=name, auth_source="local")
                s.add(u)
            u.traveler_id = traveler_id
            s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _bearer(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _weekend_trip(owner_id, name):
        with SessionLocal() as s:
            t = Trip(name=name, source="manual", owner_id=owner_id,
                     start_date="2031-10-10", end_date="2031-10-12")
            s.add(t)
            s.flush()
            ids = []
            for src in WEEKEND:
                a = TripActivity(trip_id=t.id, type=src.type, starts_at=src.starts_at,
                                 values=dict(src.values))
                s.add(a)
                s.flush()
                ids.append(a.id)
            s.commit()
            return t.id, ids

    def _clean(*tids):
        with SessionLocal() as s:
            aids = [a.id for a in s.query(TripActivity).filter(TripActivity.trip_id.in_(tids))]
            s.query(TripActivityTraveler).filter(
                TripActivityTraveler.activity_id.in_(aids)).delete(synchronize_session=False)
            s.query(TripActivity).filter(TripActivity.trip_id.in_(tids)).delete(synchronize_session=False)
            s.query(TripTraveler).filter(TripTraveler.trip_id.in_(tids)).delete(synchronize_session=False)
            s.query(TripMember).filter(TripMember.trip_id.in_(tids)).delete(synchronize_session=False)
            s.query(Trip).filter(Trip.id.in_(tids)).delete(synchronize_session=False)
            s.commit()

    def test_api_dashboard_returns_next_activity_and_in_progress():
        owner = _user("nu-owner")
        tid, (rental, hotel, _drive) = _weekend_trip(owner.id, "NU weekend")
        try:
            d = client.get("/api/v1/dashboard", params={"now": "2031-10-10T20:30"},
                           headers=_bearer(owner)).json()
            nxt = d["next_activity"]
            assert (nxt["activity"]["id"], nxt["edge"], nxt["at"], nxt["label"]) == (
                hotel, "start", "2031-10-10T22:00", "Check-in")
            assert nxt["trip_id"] == tid and nxt["trip_name"] == "NU weekend"
            assert nxt["activity"]["title"] == "Hotel Mesa Alta"
            assert [(m["activity"]["id"], m["label"], m["at"]) for m in d["in_progress"]] == [
                (rental, "Drop-off", "2031-10-12T18:00")]
            # A malformed `now` is refused rather than silently ignored.
            assert client.get("/api/v1/dashboard", params={"now": "tomorrow"},
                              headers=_bearer(owner)).status_code == 422
        finally:
            _clean(tid)

    def test_api_dashboard_moments_are_scoped_to_the_viewer():
        owner, stranger = _user("nu-owner2"), _user("nu-stranger")
        tid, _ = _weekend_trip(owner.id, "NU private")
        try:
            d = client.get("/api/v1/dashboard", params={"now": "2031-10-10T20:30"},
                           headers=_bearer(stranger)).json()
            seen = [d["next_activity"]] + d["in_progress"] if d["next_activity"] else d["in_progress"]
            assert all(m["trip_id"] != tid for m in seen)        # another user's trip: nothing
        finally:
            _clean(tid)

    def test_another_travellers_booking_is_not_my_next_up():
        # A shared trip: the hotel is tagged to traveller 301 only. Traveller 302 sees
        # the drive next; 301 sees their check-in.
        owner = _user("nu-owner3")
        tid, (_rental, hotel, drive) = _weekend_trip(owner.id, "NU shared")
        with SessionLocal() as s:
            s.add(TripActivityTraveler(activity_id=hotel, traveler_id=301))
            s.commit()
        try:
            from app.dashboard import viewer_moments
            with SessionLocal() as s:
                trips = s.query(Trip).filter(Trip.id == tid).all()
                mine, _ = viewer_moments(s, trips, "2031-10-10T20:30", NS(id="x", traveler_id=301))
                theirs, _ = viewer_moments(s, trips, "2031-10-10T20:30", NS(id="y", traveler_id=302))
            assert mine.activity.id == hotel
            assert theirs.activity.id == drive
        finally:
            _clean(tid)

    def test_home_card_shows_the_check_in_and_lists_the_rental_in_progress():
        tid, _ = _weekend_trip(None, "NU home")
        try:
            from app.dashboard import _next_up
            with SessionLocal() as s:
                trips = s.query(Trip).filter(Trip.id == tid).all()
                nu = _next_up(s, trips, "2031-10-10", now="2031-10-10T20:30")
            assert nu["kind"] == "activity" and nu["title"] == "Hotel Mesa Alta"
            assert nu["overline"] == "NEXT UP · CHECK-IN · TODAY 22:00"
            assert [(n["title"], n["label"]) for n in nu["in_progress"]] == [("Hertz", "Drop-off")]
        finally:
            _clean(tid)

    def _flight_trip(owner_id, name):
        with SessionLocal() as s:
            t = Trip(name=name, source="manual", owner_id=owner_id,
                     start_date="2031-10-11", end_date="2031-10-11")
            s.add(t)
            s.flush()
            a = TripActivity(trip_id=t.id, type="flight", starts_at="2031-10-11T07:00",
                             values={"when": "2031-10-11T07:00", "from": "PHX", "to": "DEN"})
            s.add(a)
            s.commit()
            return t.id, a.id

    def test_api_dashboard_upcoming_spans_every_trip():
        # YONDER-250: the Watch's list covers all of the viewer's trips, not just the
        # next trip's snapshot, and starts with next_activity.
        owner = _user("nu-owner4")
        tid, (rental, hotel, drive) = _weekend_trip(owner.id, "NU up weekend")
        fid, flight = _flight_trip(owner.id, "NU up flight")
        try:
            d = client.get("/api/v1/dashboard", params={"now": "2031-10-10T20:30"},
                           headers=_bearer(owner)).json()
            got = [(m["activity"]["id"], m["edge"]) for m in d["upcoming"]]
            assert got == [(hotel, "start"), (flight, "start"), (hotel, "end"),
                           (drive, "start"), (drive, "end"), (rental, "end")]
            first = d["upcoming"][0]
            assert first == d["next_activity"]
            assert {m["trip_name"] for m in d["upcoming"]} == {"NU up weekend", "NU up flight"}
            short = client.get("/api/v1/dashboard",
                               params={"now": "2031-10-10T20:30", "window_h": 12, "limit": 1},
                               headers=_bearer(owner)).json()
            assert [m["activity"]["id"] for m in short["upcoming"]] == [hotel]
            for bad in ({"window_h": 0}, {"window_h": 169}, {"limit": 0}, {"limit": 51}):
                assert client.get("/api/v1/dashboard", params=bad,
                                  headers=_bearer(owner)).status_code == 422
        finally:
            _clean(tid, fid)

    def test_api_dashboard_upcoming_is_scoped_to_the_viewer():
        # Another user's trip never appears; on a shared trip, a booking tagged to
        # another traveller isn't in my list either.
        owner, stranger = _user("nu-owner5", traveler_id=401), _user("nu-stranger5")
        tid, (_rental, hotel, _drive) = _weekend_trip(owner.id, "NU up private")
        with SessionLocal() as s:
            s.add(TripActivityTraveler(activity_id=hotel, traveler_id=402))
            s.commit()
        try:
            d = client.get("/api/v1/dashboard", params={"now": "2031-10-10T20:30"},
                           headers=_bearer(stranger)).json()
            assert all(m["trip_id"] != tid for m in d["upcoming"])
            mine = client.get("/api/v1/dashboard", params={"now": "2031-10-10T20:30"},
                              headers=_bearer(owner)).json()
            assert mine["upcoming"] and hotel not in {m["activity"]["id"] for m in mine["upcoming"]}
        finally:
            _clean(tid)
