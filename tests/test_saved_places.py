"""YONDER-200 slice 2: saved places — service + /api/v1/places, own-only scoping
(D-166) and filing to a trip. SQLite via conftest, same bootstrap as test_journal.py."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import authn
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, SavedPlace, Trip, TripActivity, User

    Base.metadata.create_all(engine)
    client = TestClient(app)

    G_PLACE = ("https://www.google.com/maps/place/Sagrada+Fam%C3%ADlia/@41.4036,2.1744,17z/"
               "data=!3m1!4b1!8m2!3d41.4036299!4d2.1743558")
    G_DIR = "https://www.google.com/maps/dir/Toronto,+ON/Ottawa,+ON/@44.7,-77.0,8z"
    A_Q = "https://maps.apple.com/?q=Le+Bernardin&ll=40.7615,-73.9817&address=155+W+51st+St"

    def _user(name):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                u = User(username=name, password_hash=authn.hash_password("pw"),
                         auth_source="local")
                s.add(u)
                s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _bearer(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _trip(name, owner_id):
        with SessionLocal() as s:
            t = Trip(name=name, source="manual", owner_id=owner_id)
            s.add(t)
            s.commit()
            s.refresh(t)
            s.expunge(t)
            return t

    def _cleanup(*usernames):
        with SessionLocal() as s:
            ids = [u.id for u in s.query(User).filter(User.username.in_(usernames)).all()]
            if ids:
                trips = [t.id for t in s.query(Trip).filter(Trip.owner_id.in_(ids)).all()]
                if trips:
                    s.query(TripActivity).filter(TripActivity.trip_id.in_(trips)).delete(
                        synchronize_session=False)
                s.query(SavedPlace).filter(SavedPlace.owner_id.in_(ids)).delete(
                    synchronize_session=False)
                s.query(Trip).filter(Trip.owner_id.in_(ids)).delete(
                    synchronize_session=False)
            s.commit()

    def test_save_from_google_url_parses_name_pin_and_keeps_link():
        u = _user("sp_a")
        try:
            r = client.post("/api/v1/places", json={"url": G_PLACE}, headers=_bearer(u))
            assert r.status_code == 201, r.text
            p = r.json()
            assert p["name"] == "Sagrada Família" and p["provider"] == "google"
            assert (p["lat"], p["lon"]) == (41.4036299, 2.1743558)
            assert p["source_url"] == G_PLACE and p["activity_type"] == "tour"   # no category
        finally:
            _cleanup("sp_a")

    def test_client_resolved_fields_win_and_map_category_to_type():
        """iOS sends what MapKit resolved; the server does no lookup (D-166)."""
        u = _user("sp_a")
        try:
            r = client.post("/api/v1/places", headers=_bearer(u), json={
                "url": A_Q, "name": "Le Bernardin NYC", "address": "155 W 51st St, NY",
                "category": "MKPOICategoryRestaurant", "source": "ios"})
            p = r.json()
            assert p["name"] == "Le Bernardin NYC" and p["provider"] == "apple"
            assert p["address"] == "155 W 51st St, NY"
            assert p["activity_type"] == "dining" and p["source"] == "ios"
        finally:
            _cleanup("sp_a")

    def test_save_without_url_needs_name_or_pin():
        u = _user("sp_a")
        try:
            h = _bearer(u)
            assert client.post("/api/v1/places", json={}, headers=h).status_code == 400
            assert client.post("/api/v1/places", json={"lat": 1.0}, headers=h).status_code == 400
            pin = client.post("/api/v1/places", json={"lat": 41.4, "lon": 2.17}, headers=h)
            assert pin.status_code == 201 and pin.json()["name"] == "41.40000, 2.17000"
        finally:
            _cleanup("sp_a")

    def test_directions_and_junk_urls_are_refused():
        u = _user("sp_a")
        try:
            for bad in (G_DIR, "https://example.com/x", "not a url"):
                r = client.post("/api/v1/places", json={"url": bad}, headers=_bearer(u))
                assert r.status_code == 400, bad
        finally:
            _cleanup("sp_a")

    def test_resaving_the_same_place_is_idempotent():
        u = _user("sp_a")
        try:
            h = _bearer(u)
            a = client.post("/api/v1/places", json={"url": G_PLACE}, headers=h)
            b = client.post("/api/v1/places", json={"url": G_PLACE}, headers=h)
            assert a.status_code == 201 and b.status_code == 200
            assert a.json()["id"] == b.json()["id"]
            assert len(client.get("/api/v1/places", headers=h).json()) == 1
        finally:
            _cleanup("sp_a")

    def test_places_are_own_only():
        """The scoping test (AGENTS.md): another user can neither see, delete nor
        file a place — and an admin gets no special view of someone's wishlist."""
        a, b = _user("sp_a"), _user("sp_b")
        try:
            pid = client.post("/api/v1/places", json={"url": G_PLACE},
                              headers=_bearer(a)).json()["id"]
            assert client.get("/api/v1/places", headers=_bearer(b)).json() == []
            assert client.delete(f"/api/v1/places/{pid}", headers=_bearer(b)).status_code == 404
            tb = _trip("B trip", b.id)
            r = client.post(f"/api/v1/places/{pid}/file/{tb.id}", json={}, headers=_bearer(b))
            assert r.status_code == 404                       # not b's place
            assert len(client.get("/api/v1/places", headers=_bearer(a)).json()) == 1
            # the same place saved by b is b's own row, not a's
            other = client.post("/api/v1/places", json={"url": G_PLACE}, headers=_bearer(b))
            assert other.status_code == 201 and other.json()["id"] != pid
        finally:
            _cleanup("sp_a", "sp_b")

    def test_requires_auth():
        assert client.get("/api/v1/places").status_code in (401, 403)
        assert client.post("/api/v1/places", json={"url": G_PLACE}).status_code in (401, 403)

    def test_file_to_trip_makes_an_undated_activity_and_consumes_the_place():
        u = _user("sp_a")
        try:
            h = _bearer(u)
            t = _trip("Barcelona", u.id)
            pid = client.post("/api/v1/places", headers=h, json={
                "url": A_Q, "category": "amenity=restaurant"}).json()["id"]
            r = client.post(f"/api/v1/places/{pid}/file/{t.id}", json={}, headers=h)
            assert r.status_code == 201, r.text
            a = r.json()
            assert a["type"] == "dining" and a["values"]["name"] == "Le Bernardin"
            assert a["values"]["address"] == "155 W 51st St"
            assert a["values"]["mapLink"] == A_Q          # the person's own link (YONDER-135)
            assert not a["starts_at"]                      # undated: scheduled later
            assert client.get("/api/v1/places", headers=h).json() == []   # consumed
            with SessionLocal() as s:
                assert s.query(TripActivity).filter_by(trip_id=t.id).count() == 1
        finally:
            _cleanup("sp_a")

    def test_file_with_date_and_type_override():
        u = _user("sp_a")
        try:
            h = _bearer(u)
            t = _trip("Trip", u.id)
            pid = client.post("/api/v1/places", json={"url": G_PLACE}, headers=h).json()["id"]
            r = client.post(f"/api/v1/places/{pid}/file/{t.id}", headers=h, json={
                "starts_at": "2026-10-12T10:00", "activity_type": "event",
                "notes": "book ahead"})
            assert r.status_code == 201, r.text
            assert r.json()["type"] == "event" and r.json()["starts_at"] == "2026-10-12T10:00"
            assert r.json()["values"]["notes"] == "book ahead"
        finally:
            _cleanup("sp_a")

    def test_file_refuses_a_trip_you_cannot_edit_and_keeps_the_place():
        a, b = _user("sp_a"), _user("sp_b")
        try:
            pid = client.post("/api/v1/places", json={"url": G_PLACE},
                              headers=_bearer(a)).json()["id"]
            tb = _trip("B's trip", b.id)                  # a has no access to it
            r = client.post(f"/api/v1/places/{pid}/file/{tb.id}", json={}, headers=_bearer(a))
            assert r.status_code in (403, 404)
            r = client.post(f"/api/v1/places/{pid}/file/nope", json={}, headers=_bearer(a))
            assert r.status_code == 404
            r = client.post(f"/api/v1/places/{pid}/file/{_trip('mine', a.id).id}",
                            json={"activity_type": "spaceship"}, headers=_bearer(a))
            assert r.status_code == 400
            assert len(client.get("/api/v1/places", headers=_bearer(a)).json()) == 1
        finally:
            _cleanup("sp_a", "sp_b")

    def test_delete_own_place():
        u = _user("sp_a")
        try:
            h = _bearer(u)
            pid = client.post("/api/v1/places", json={"url": G_PLACE}, headers=h).json()["id"]
            assert client.delete(f"/api/v1/places/{pid}", headers=h).status_code == 204
            assert client.get("/api/v1/places", headers=h).json() == []
        finally:
            _cleanup("sp_a")

    # ---------------------------------------------------- hardening (review of this PR)
    def test_only_http_links_are_kept_as_a_places_link():
        """source_url is rendered as an href and becomes the activity's Map button."""
        u = _user("sp_a")
        try:
            h = _bearer(u)
            r = client.post("/api/v1/places", headers=h, json={
                "name": "X", "source_url": "javascript:alert(1)"})
            assert r.status_code == 201 and r.json()["source_url"] is None
            ok = client.post("/api/v1/places", headers=h, json={
                "name": "Y", "source_url": "https://maps.apple.com/?q=Y"})
            assert ok.json()["source_url"] == "https://maps.apple.com/?q=Y"
            # a place is not a place to keep any other site's link
            other = client.post("/api/v1/places", headers=h, json={
                "name": "Z", "source_url": "https://example.com/anything"})
            assert other.status_code == 201 and other.json()["source_url"] is None
        finally:
            _cleanup("sp_a")

    def test_file_rejects_a_malformed_starts_at_and_keeps_the_place():
        u = _user("sp_a")
        try:
            h = _bearer(u)
            t = _trip("T", u.id)
            pid = client.post("/api/v1/places", json={"url": G_PLACE}, headers=h).json()["id"]
            for bad in ("tomorrow", "2026-13-01x", "2026-10-12 10:00"):
                r = client.post(f"/api/v1/places/{pid}/file/{t.id}", json={"starts_at": bad},
                                headers=h)
                assert r.status_code == 400, bad
            assert len(client.get("/api/v1/places", headers=h).json()) == 1
        finally:
            _cleanup("sp_a")

    # ----------------------------------------------------------------- rename / edit
    def test_patch_renames_and_edits_own_place_only():
        a, b = _user("sp_a"), _user("sp_b")
        try:
            pid = client.post("/api/v1/places", json={"lat": 41.4, "lon": 2.17},
                              headers=_bearer(a)).json()["id"]
            r = client.patch(f"/api/v1/places/{pid}", headers=_bearer(a), json={
                "name": "Sagrada Família", "address": "Carrer de Mallorca 401",
                "activity_type": "event", "notes": "book ahead"})
            assert r.status_code == 200, r.text
            p = r.json()
            assert (p["name"], p["activity_type"], p["notes"]) == (
                "Sagrada Família", "event", "book ahead")
            assert p["lat"] == 41.4                                  # untouched
            assert client.patch(f"/api/v1/places/{pid}", headers=_bearer(b),
                                json={"name": "mine now"}).status_code == 404
            assert client.patch(f"/api/v1/places/{pid}", headers=_bearer(a),
                                json={"name": "  "}).status_code == 400
            assert client.patch(f"/api/v1/places/{pid}", headers=_bearer(a),
                                json={"activity_type": "spaceship"}).status_code == 400
        finally:
            _cleanup("sp_a", "sp_b")

    # ---------------------------------------------------------------- trip suggestion
    def _located_activity(trip_id, lat, lon):
        with SessionLocal() as s:
            s.add(TripActivity(trip_id=trip_id, type="dining", values={"name": "Dinner"},
                               geo={"points": [{"role": "place", "lat": lat, "lon": lon}]},
                               starts_at=""))
            s.commit()

    def _set_dates(trip_id, start, end):
        with SessionLocal() as s:
            t = s.get(Trip, trip_id)
            t.start_date, t.end_date = start, end
            s.commit()

    def test_suggests_the_trip_whose_stops_are_nearest_the_pin():
        u = _user("sp_a")
        try:
            h = _bearer(u)
            far, near = _trip("Paris", u.id), _trip("Barcelona", u.id)
            _set_dates(far.id, "2099-01-01", "2099-01-05")
            _set_dates(near.id, "2099-06-01", "2099-06-05")
            _located_activity(far.id, 48.85, 2.35)
            _located_activity(near.id, 41.39, 2.16)            # ~2 km from the Sagrada Família
            client.post("/api/v1/places", json={"url": G_PLACE}, headers=h)
            p = client.get("/api/v1/places", headers=h).json()[0]
            assert p["suggested_trip_id"] == near.id
        finally:
            _cleanup("sp_a")

    def test_suggestion_falls_back_to_current_then_next_and_never_a_past_trip():
        from datetime import date, timedelta
        u = _user("sp_a")
        try:
            h = _bearer(u)
            today = date.today()
            past = _trip("Past", u.id)
            _set_dates(past.id, (today - timedelta(days=30)).isoformat(),
                       (today - timedelta(days=20)).isoformat())
            _located_activity(past.id, 41.40, 2.17)            # right on the pin, but over
            client.post("/api/v1/places", json={"url": G_PLACE}, headers=h)
            assert client.get("/api/v1/places", headers=h).json()[0]["suggested_trip_id"] is None
            nxt = _trip("Next", u.id)
            _set_dates(nxt.id, (today + timedelta(days=10)).isoformat(),
                       (today + timedelta(days=14)).isoformat())
            assert client.get("/api/v1/places", headers=h).json()[0]["suggested_trip_id"] == nxt.id
            cur = _trip("Now", u.id)
            _set_dates(cur.id, (today - timedelta(days=1)).isoformat(),
                       (today + timedelta(days=2)).isoformat())
            assert client.get("/api/v1/places", headers=h).json()[0]["suggested_trip_id"] == cur.id
        finally:
            _cleanup("sp_a")

    def test_a_name_only_place_still_gets_a_date_based_suggestion():
        from datetime import date, timedelta
        u = _user("sp_a")
        try:
            h = _bearer(u)
            t = _trip("Soon", u.id)
            _set_dates(t.id, (date.today() + timedelta(days=3)).isoformat(),
                       (date.today() + timedelta(days=6)).isoformat())
            client.post("/api/v1/places", json={"name": "Some cafe"}, headers=h)
            assert client.get("/api/v1/places", headers=h).json()[0]["suggested_trip_id"] == t.id
        finally:
            _cleanup("sp_a")
