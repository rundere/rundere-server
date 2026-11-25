"""D-250 — a new account that types an existing traveller's name asks an admin
to link it, instead of being refused.

Nothing is linked until an admin approves (D-204 holds). The person carries on
unlinked, the admins are told (username only, D-197), and the Users page shows
the request with Link and Dismiss. Link promotes a placeholder guest (D-100) in
the same step, and is refused when another account already is that traveller.
"""
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
    import pytest

    from app import accounts_routes, authn, notify
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import AccountEvent, Base, TravelerIdentity, Trip, TripTraveler, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    NAMES = ("Lr Robin Example", "Lr Sam Sample")

    @pytest.fixture()
    def sent(monkeypatch):
        """Capture emails and admin alerts instead of sending them."""
        out = {"email": [], "alert": []}
        monkeypatch.setattr(accounts_routes, "send_email",
                            lambda to, subject, body: out["email"].append((to, subject, body)))
        monkeypatch.setattr(notify, "dispatch",
                            lambda cat, title, msg, **kw: out["alert"].append((cat, title, msg)))
        yield out
        with SessionLocal() as s:
            uids = [u.id for u in s.query(User).filter(User.username.like("lr-%"))]
            tids = [t.id for t in s.query(Trip).filter(Trip.name.like("Lr %"))]
            s.query(TripTraveler).filter(TripTraveler.trip_id.in_(tids)).delete(
                synchronize_session=False)
            s.query(Trip).filter(Trip.id.in_(tids)).delete(synchronize_session=False)
            s.query(AccountEvent).filter(AccountEvent.user_id.in_(uids)).delete(
                synchronize_session=False)
            s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
            s.query(TravelerIdentity).filter(TravelerIdentity.display_name.in_(NAMES)).delete(
                synchronize_session=False)
            s.commit()

    def _traveller(name, generic=False):
        with SessionLocal() as s:
            t = TravelerIdentity(display_name=name, kind="guest", generic=generic)
            s.add(t)
            s.flush()
            # SQLite reuses a deleted traveller's id; another file's leftover user
            # can still point at it, which the "already linked" guard would see.
            s.query(User).filter(User.traveler_id == t.id,
                                 ~User.username.like("lr-%")).update(
                {User.traveler_id: None}, synchronize_session=False)
            s.commit()
            return t.id

    def _user(name, admin=False, traveler_id=None, email=None):
        with SessionLocal() as s:
            u = User(username=name, auth_source="local", is_admin=admin,
                     traveler_id=traveler_id, email=email)
            s.add(u)
            s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _h(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _get(uid):
        with SessionLocal() as s:
            u = s.get(User, uid)
            s.expunge(u)
            return u

    def _trip_with(traveler_id, owner):
        with SessionLocal() as s:
            t = Trip(name="Lr Lisbon", source="manual", owner_id=owner.id)
            s.add(t)
            s.flush()
            s.add(TripTraveler(trip_id=t.id, traveler_id=traveler_id, role="companion"))
            s.commit()
            return t.id

    def _my_trip_ids(u):
        return {t["id"] for t in client.get("/api/v1/trips", headers=_h(u)).json()}

    # ------------------------------------------------------------------ asking
    def test_taken_name_asks_then_requests_and_tells_the_admins(sent):
        guest = _traveller("Lr Robin Example", generic=True)       # quick-added on a trip
        owner = _user("lr-owner")
        tid = _trip_with(guest, owner)
        _user("lr-admin", admin=True, email="admin@example.test")
        me = _user("lr-robin")
        r = client.post("/account/traveller", headers=_h(me), follow_redirects=False,
                        data={"new_traveler": "lr robin example", "next": "/"})
        assert r.status_code == 200 and "Is that you?" in r.text
        assert "Lr Robin Example" in r.text
        assert _get(me.id).traveler_id is None and _get(me.id).requested_traveler_id is None
        assert sent["alert"] == [] and sent["email"] == []            # nothing until "yes"

        r = client.post("/account/traveller/request", headers=_h(me), follow_redirects=False,
                        data={"name": "Lr Robin Example", "next": "/"})
        assert r.status_code == 303 and r.headers["location"] == "/"   # carries on
        u = _get(me.id)
        assert u.requested_traveler_id == guest and u.traveler_id is None
        # admins are told, by username only: never the traveller's name (D-197)
        assert [a[0] for a in sent["alert"]] == ["accounts"]
        assert "lr-robin" in sent["alert"][0][2] and "Robin Example" not in sent["alert"][0][2]
        assert [e[0] for e in sent["email"]] == ["admin@example.test"]
        assert "Robin Example" not in sent["email"][0][2]
        # asking again for the same traveller doesn't alert again
        client.post("/account/traveller/request", headers=_h(me),
                    data={"name": "Lr Robin Example", "next": "/"})
        assert len(sent["alert"]) == 1
        # D-204 holds: a request gives no access to the traveller's trips
        assert tid not in _my_trip_ids(me)
        # the page shows the wait, and the request can be cancelled
        page = client.get("/account/traveller", headers=_h(me)).text
        assert "Waiting for an admin to link you to Lr Robin Example" in page
        client.post("/account/traveller/request/cancel", headers=_h(me))
        assert _get(me.id).requested_traveler_id is None

    def test_unknown_name_requests_nothing(sent):
        me = _user("lr-nobody")
        r = client.post("/account/traveller/request", headers=_h(me), follow_redirects=False,
                        data={"name": "Lr Not A Traveller", "next": "/"})
        assert r.status_code == 303 and "/account/traveller" in r.headers["location"]
        assert _get(me.id).requested_traveler_id is None and sent["alert"] == []

    def test_register_with_a_taken_name_signs_in_and_asks(sent, monkeypatch):
        _traveller("Lr Sam Sample")
        monkeypatch.setattr(settings, "auth_registration_open", True)
        c = TestClient(app)
        r = c.post("/register", follow_redirects=False,
                   data={"username": "lr-sam", "password": "pw-long-enough",
                         "email": "sam@example.test", "new_traveler": "Lr Sam Sample"})
        assert r.status_code == 200 and "Is that you?" in r.text
        assert settings.auth_cookie_name in r.cookies                   # signed in
        with SessionLocal() as s:
            u = s.query(User).filter_by(username="lr-sam").one()
            assert u.traveler_id is None and u.requested_traveler_id is None

    # ------------------------------------------------------------------ answering
    def _asked(name="lr-asker", traveller="Lr Robin Example", generic=True, email=None):
        tid = _traveller(traveller, generic=generic)
        me = _user(name, email=email)
        client.post("/account/traveller/request", headers=_h(me),
                    data={"name": traveller, "next": "/"})
        return me, tid

    def test_users_page_shows_it_and_link_promotes_and_links_in_one_step(sent):
        admin = _user("lr-admin2", admin=True)
        me, guest = _asked(email="robin@example.test")
        owner = _user("lr-owner2")
        trip = _trip_with(guest, owner)
        page = client.get("/users", headers=_h(admin)).text
        assert "Wants to be linked to" in page and "Lr Robin Example" in page
        assert "a guest added from a trip" in page
        # only an admin can answer it
        assert client.post(f"/users/{me.id}/traveler/approve", headers=_h(me),
                           follow_redirects=False).status_code == 403
        r = client.post(f"/users/{me.id}/traveler/approve", headers=_h(admin),
                        follow_redirects=False)
        assert r.status_code == 303 and "msg=" in r.headers["location"]
        u = _get(me.id)
        assert u.traveler_id == guest and u.requested_traveler_id is None
        with SessionLocal() as s:
            assert s.get(TravelerIdentity, guest).generic is False      # promoted (D-250)
            acts = [e.action for e in s.query(AccountEvent).filter_by(user_id=me.id)]
            assert {"link_requested", "traveller_promoted", "traveller_linked"} <= set(acts)
        assert any(to == "robin@example.test" for to, _, _ in sent["email"])
        assert trip in _my_trip_ids(me)                                  # now theirs too
        assert "Wants to be linked to" not in client.get("/users", headers=_h(admin)).text

    def test_link_refused_when_another_account_is_that_traveller(sent):
        admin = _user("lr-admin3", admin=True)
        me, tid = _asked(name="lr-second", generic=False)
        _user("lr-first", traveler_id=tid)
        r = client.post(f"/users/{me.id}/traveler/approve", headers=_h(admin),
                        follow_redirects=False)
        assert "already+linked" in r.headers["location"] or "already%20linked" in r.headers["location"]
        u = _get(me.id)
        assert u.traveler_id is None and u.requested_traveler_id == tid     # still open

    def test_dismiss_clears_it_and_tells_them(sent):
        admin = _user("lr-admin4", admin=True)
        me, tid = _asked(name="lr-dismissed", email="d@example.test")
        r = client.post(f"/users/{me.id}/traveler/dismiss", headers=_h(admin),
                        follow_redirects=False)
        assert r.status_code == 303
        u = _get(me.id)
        assert u.requested_traveler_id is None and u.traveler_id is None
        assert any(to == "d@example.test" and "declined" in body for to, _, body in sent["email"])
        with SessionLocal() as s:
            assert s.get(TravelerIdentity, tid).generic is True          # untouched

    def test_a_manual_link_answers_the_request(sent):
        admin = _user("lr-admin5", admin=True)
        me, _ = _asked(name="lr-manual", generic=False)
        other = _traveller("Lr Sam Sample")
        client.post(f"/users/{me.id}/traveler", headers=_h(admin),
                    data={"traveler_id": str(other), "new_traveler": ""})
        u = _get(me.id)
        assert u.traveler_id == other and u.requested_traveler_id is None
