"""Yonder 4.1 — users share their data with admins by default, and can opt out.

Sharing (default): an admin sees and edits the user's trips, captures, journal.
Opted out: the admin sees only what the user invites them into; the admin
can't set their password or email, and reset links are emailed to the user.
Self-registered/SSO users are asked at first sign-in. SQLite via conftest.
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
    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (AccountEvent, Base, JournalEntry, TravelerIdentity,
                            TravelItem, Trip, TripTraveler, User)

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    def _setup(sister_email="sis@example.com", sister_source="local"):
        with SessionLocal() as s:
            _teardown(s)
            me_t = TravelerIdentity(display_name="AS Me", kind="guest")
            sis_t = TravelerIdentity(display_name="AS Sis", kind="guest")
            s.add_all([me_t, sis_t]); s.flush()
            admin = User(username="as-admin", password_hash=authn.hash_password("pw"),
                         auth_source="local", is_admin=True, traveler_id=me_t.id)
            sis = User(username="as-sis", password_hash=authn.hash_password("pw"),
                       auth_source=sister_source, traveler_id=sis_t.id, email=sister_email)
            s.add_all([admin, sis]); s.flush()
            hers = Trip(name="AS Her Trip", source="manual", owner_id=sis.id)
            invited = Trip(name="AS Her Invite", source="manual", owner_id=sis.id)
            mine_for_her = Trip(name="AS Set Up For Her", source="manual", owner_id=admin.id)
            s.add_all([hers, invited, mine_for_her]); s.flush()
            s.add_all([TripTraveler(trip_id=invited.id, traveler_id=me_t.id, role="companion"),
                       TripTraveler(trip_id=mine_for_her.id, traveler_id=sis_t.id, role="companion")])
            cap = TravelItem(owner_id=sis.id, canonical_key="as:cap", source={}, status={})
            note = JournalEntry(owner_id=sis.id, entry_date="2027-05-01", body="as-her-diary",
                                source="web")
            s.add_all([cap, note]); s.commit()
            return {"admin": admin.id, "sis": sis.id, "hers": hers.id, "invited": invited.id,
                    "mine_for_her": mine_for_her.id, "cap": cap.id, "note": note.id}

    def _teardown(s):
        tids = [t.id for t in s.query(Trip).filter(Trip.name.like("AS %")).all()]
        s.query(TripTraveler).filter(TripTraveler.trip_id.in_(tids)).delete(synchronize_session=False)
        s.query(Trip).filter(Trip.id.in_(tids)).delete(synchronize_session=False)
        s.query(TravelItem).filter_by(canonical_key="as:cap").delete()
        s.query(JournalEntry).filter_by(body="as-her-diary").delete()
        uids = [u.id for u in s.query(User).filter(User.username.like("as-%")).all()]
        s.query(AccountEvent).filter(AccountEvent.user_id.in_(uids)).delete(synchronize_session=False)
        s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
        s.query(TravelerIdentity).filter(TravelerIdentity.display_name.like("AS %")).delete(
            synchronize_session=False)
        s.commit()

    def _h(username):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=username).one()
            return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _with_smtp(monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_from", "yonder@example.com")
        sent = []
        from app import accounts_routes
        monkeypatch.setattr(accounts_routes, "send_email",
                            lambda to, subj, body: sent.append((to, subj, body)) or True)
        return sent

    def _visible_to_admin(ids):
        h = _h("as-admin")
        return {
            "trip": client.get(f"/trips/{ids['hers']}", headers=h).status_code == 200,
            "invited": client.get(f"/trips/{ids['invited']}", headers=h).status_code == 200,
            "capture": client.get(f"/review/item/{ids['cap']}", headers=h).status_code == 200,
            "journal": "as-her-diary" in client.get("/journal", headers=h).text,
        }

    def _run(fn, *a, **kw):
        ids = _setup(**kw)
        settings.auth_required = True
        try:
            fn(ids, *a)
        finally:
            settings.auth_required = False
            with SessionLocal() as s:
                _teardown(s)

    def test_sharing_by_default_then_opt_out_hides_her_data(monkeypatch):
        _with_smtp(monkeypatch)

        def body(ids):
            assert _visible_to_admin(ids) == {"trip": True, "invited": True,
                                              "capture": True, "journal": True}
            r = client.post("/account/sharing", headers=_h("as-sis"),
                            data={"choice": "private", "next": "/account"},
                            follow_redirects=False)
            assert "err=" not in r.headers["location"]
            # opted out: only the trip she invited him into stays visible
            assert _visible_to_admin(ids) == {"trip": False, "invited": True,
                                              "capture": False, "journal": False}
            # a trip the admin set up for her is still hers to use (roster) and his
            assert client.get(f"/trips/{ids['mine_for_her']}",
                              headers=_h("as-sis")).status_code == 200
            assert client.get(f"/trips/{ids['mine_for_her']}",
                              headers=_h("as-admin")).status_code == 200
            with SessionLocal() as s:
                assert [e.action for e in s.query(AccountEvent)
                        .filter_by(user_id=ids["sis"]).all()] == ["sharing_off"]
            # opting back in restores access
            client.post("/account/sharing", headers=_h("as-sis"), data={"choice": "share"})
            assert _visible_to_admin(ids)["trip"] is True
        _run(body)

    def test_opt_out_needs_email_delivery_for_local_accounts(monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", "")

        def body(ids):
            r = client.post("/account/sharing", headers=_h("as-sis"),
                            data={"choice": "private", "next": "/account"},
                            follow_redirects=False)
            assert "err=" in r.headers["location"]
            with SessionLocal() as s:
                assert s.get(User, ids["sis"]).share_with_admins is True
        _run(body)

    def test_sso_account_can_opt_out_without_smtp(monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", "")

        def body(ids):
            client.post("/account/sharing", headers=_h("as-sis"),
                        data={"choice": "private"})
            with SessionLocal() as s:
                assert s.get(User, ids["sis"]).share_with_admins is False
        _run(body, sister_source="oidc", sister_email=None)

    def test_admin_account_powers_over_an_opted_out_user(monkeypatch):
        sent = _with_smtp(monkeypatch)

        def body(ids):
            client.post("/account/sharing", headers=_h("as-sis"), data={"choice": "private"})
            h = _h("as-admin")
            # can't set her password or email
            r = client.post(f"/users/{ids['sis']}/password", headers=h,
                            data={"password": "hijack"}, follow_redirects=False)
            assert "err=" in r.headers["location"]
            r = client.post(f"/users/{ids['sis']}/email", headers=h,
                            data={"email": "admin@example.com"}, follow_redirects=False)
            assert "err=" in r.headers["location"]
            with SessionLocal() as s:
                sis = s.get(User, ids["sis"])
                assert authn.verify_password("pw", sis.password_hash)
                assert sis.email == "sis@example.com"
            # a reset link is emailed to HER and never shown to the admin
            r = client.post(f"/users/{ids['sis']}/reset-link", headers=h,
                            follow_redirects=False)
            assert "link=" not in r.headers["location"] and "emailed" in r.headers["location"]
            assert sent and sent[-1][0] == "sis@example.com" and "/reset/" in sent[-1][2]
            # deactivating her is still allowed, and she's told about it
            client.post(f"/users/{ids['sis']}/active", headers=h)
            assert sent[-1][0] == "sis@example.com" and "deactivated" in sent[-1][2]
            with SessionLocal() as s:
                acts = {e.action for e in s.query(AccountEvent).filter_by(user_id=ids["sis"]).all()}
                assert {"sharing_off", "reset_link_emailed", "deactivated"} <= acts
            # and she can't strip the email her resets rely on
            client.post(f"/users/{ids['sis']}/active", headers=h)          # reactivate
            r = client.post("/account/email", headers=_h("as-sis"), follow_redirects=False,
                            data={"email": "", "current_password": "pw"})
            assert "err=" in r.headers["location"]
        _run(body)

    def test_self_registered_user_is_asked_at_first_sign_in(monkeypatch):
        monkeypatch.setattr(settings, "auth_registration_open", True)
        settings.auth_required = True
        try:
            with SessionLocal() as s:
                s.query(User).filter_by(username="as-newbie").delete(); s.commit()
            c = TestClient(app)
            c.post("/register", data={"username": "as-newbie", "password": "pw",
                                      "traveler_id": "none", "new_traveler": ""})
            r = c.get("/trips", follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"].startswith("/account/sharing")
            assert "Share your data with admins" in c.get("/account/sharing").text
            # the JSON API is never redirected
            with SessionLocal() as s:
                u = s.query(User).filter_by(username="as-newbie").one()
                tok = authn.issue_access_token(u)
            assert client.get("/api/v1/trips",
                              headers={"Authorization": f"Bearer {tok}"}).status_code == 200
            c.post("/account/sharing", data={"choice": "share", "next": "/trips"})
            assert c.get("/trips", follow_redirects=False).status_code == 200
            with SessionLocal() as s:
                u = s.query(User).filter_by(username="as-newbie").one()
                assert u.sharing_prompt is False and u.share_with_admins is True
        finally:
            settings.auth_required = False
            with SessionLocal() as s:
                s.query(User).filter_by(username="as-newbie").delete(); s.commit()

    def test_admin_created_users_share_without_being_asked():
        with SessionLocal() as s:
            s.query(User).filter_by(username="as-invited").delete(); s.commit()
            u = authn.create_user(s, "as-invited", "pw")
            assert u.share_with_admins is True and u.sharing_prompt is False
            s.delete(u); s.commit()
