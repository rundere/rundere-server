"""Yonder 4.1 admin sharing, private by default since D-220 (YONDER-335).

Private (default): the admin sees only what the user invites them into; the
admin can't set their password or email, and reset links are emailed to the
user. Opted in: an admin sees and edits the user's trips, captures, journal.
Self-registered/SSO users are asked at first sign-in, and a local account adds
an email before anything else. SQLite via conftest.
"""
import sys
from pathlib import Path

import pytest

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

    def _setup(sister_email="sis@example.com", sister_source="local", sister_shares=True):
        with SessionLocal() as s:
            _teardown(s)
            me_t = TravelerIdentity(display_name="AS Me", kind="guest")
            sis_t = TravelerIdentity(display_name="AS Sis", kind="guest")
            s.add_all([me_t, sis_t]); s.flush()
            admin = User(username="as-admin", password_hash=authn.hash_password("pw"),
                         auth_source="local", is_admin=True, traveler_id=me_t.id)
            # She opted in to sharing unless a test says otherwise (D-220: new
            # accounts start private).
            sis = User(username="as-sis", password_hash=authn.hash_password("pw"),
                       auth_source=sister_source, traveler_id=sis_t.id, email=sister_email,
                       share_with_admins=sister_shares)
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
        from app import email as mail
        fake = lambda to, subj, body: sent.append((to, subj, body)) or True  # noqa: E731
        monkeypatch.setattr(accounts_routes, "send_email", fake)
        monkeypatch.setattr(mail, "send_email", fake)            # support.py (D-211)
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

    def test_an_opted_in_user_is_visible_then_opting_out_hides_her_data(monkeypatch):
        _with_smtp(monkeypatch)

        def body(ids):
            # D-211: support access shows trips and the Inbox, never the journal
            assert _visible_to_admin(ids) == {"trip": True, "invited": True,
                                              "capture": True, "journal": False}
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
                acts = [e.action for e in s.query(AccountEvent).filter_by(user_id=ids["sis"])]
                assert "sharing_off" in acts and acts.count("sharing_off") == 1
            # opting back in restores access
            client.post("/account/sharing", headers=_h("as-sis"), data={"choice": "share"})
            assert _visible_to_admin(ids)["trip"] is True
        _run(body)

    def test_a_new_private_user_shows_the_admin_only_what_she_invites_him_into(monkeypatch):
        """D-220 (YONDER-335): the default. Nothing of hers is visible to an
        admin until she opts in, except the trip she put him on."""
        _with_smtp(monkeypatch)

        def body(ids):
            assert _visible_to_admin(ids) == {"trip": False, "invited": True,
                                              "capture": False, "journal": False}
            mine = {t["name"] for t in client.get("/api/v1/trips", headers=_h("as-admin")).json()}
            assert "AS Her Trip" not in mine and "AS Her Invite" in mine
            client.post("/account/sharing", headers=_h("as-sis"), data={"choice": "share"})
            assert _visible_to_admin(ids) == {"trip": True, "invited": True,
                                              "capture": True, "journal": False}
        _run(body, sister_shares=False)

    def test_going_private_needs_no_email_and_says_what_that_means(monkeypatch):
        """D-220: going private never waits on server email or an address (that
        used to leave someone stuck sharing). The account page says that a
        forgotten password then needs whoever runs the server."""
        monkeypatch.setattr(settings, "smtp_host", "")

        def body(ids):
            r = client.post("/account/sharing", headers=_h("as-sis"),
                            data={"choice": "private", "next": "/account"},
                            follow_redirects=False)
            assert "err=" not in r.headers["location"]
            with SessionLocal() as s:
                assert s.get(User, ids["sis"]).share_with_admins is False
            assert "only whoever runs the server" in client.get(
                "/account", headers=_h("as-sis")).text
        _run(body, sister_email=None)

    def test_a_blank_sharing_choice_stays_private(monkeypatch):
        def body(ids):
            client.post("/account/sharing", headers=_h("as-sis"), data={"next": "/account"})
            with SessionLocal() as s:
                assert s.get(User, ids["sis"]).share_with_admins is False
        _run(body, sister_shares=False)

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
            # and she can't strip the email her resets rely on (D-220: a local
            # account always has one)
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
                                      "email": "newbie@example.com",
                                      "traveler_id": "none", "new_traveler": ""})
            r = c.get("/trips", follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"].startswith("/account/sharing")
            page = c.get("/account/sharing").text
            assert "Let admins see your trips" in page
            assert 'value="private" checked' in page          # D-220: private is the default
            with SessionLocal() as s:
                assert s.query(User).filter_by(username="as-newbie").one().share_with_admins is False
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

    def test_every_way_an_account_is_made_starts_private(monkeypatch):
        """D-220: admin-created (web and CLI), self-registered, SSO, the trusted
        proxy header and the demo login all start with sharing off. Only the
        self-made ones are asked."""
        from app import cli, demo
        names = ("as-invited", "as-cli", "as-sso", "as-proxy", "as-demo")

        def wipe():
            with SessionLocal() as s:
                s.query(User).filter(User.username.in_(names)).delete(
                    synchronize_session=False)
                s.commit()
        wipe()
        try:
            with SessionLocal() as s:
                made = {"as-invited": authn.create_user(s, "as-invited", "pw")}
                monkeypatch.setattr(settings, "oidc_auto_create", True)
                made["as-sso"] = authn.provision_oidc_user(s, sub="as-sso-1", username="as-sso")
                monkeypatch.setattr(settings, "auth_proxy_header_trust", True)

                class _Req:
                    headers = {"x-authentik-username": "as-proxy"}
                made["as-proxy"] = authn.resolve_proxy_user(s, _Req())
                monkeypatch.setattr(settings, "demo_username", "as-demo")
                monkeypatch.setattr(settings, "demo_password", "demo-pw")
                made["as-demo"] = demo.ensure_demo_user(s)
            assert cli.main(["create-user", "as-cli", "--password", "pw"]) == 0
            with SessionLocal() as s:
                rows = {u.username: u for u in s.query(User).filter(User.username.in_(names))}
            assert set(rows) == set(names)
            assert all(u.share_with_admins is False for u in rows.values())
            assert {n for n, u in rows.items() if u.sharing_prompt} == {"as-sso", "as-proxy"}
        finally:
            wipe()

    def test_migration_0045_keeps_existing_choices_and_defaults_new_rows_private(tmp_path):
        """Run the real alembic chain: accounts made before 0045 keep what they
        had (a sharer stays a sharer); a row inserted after it starts private.
        (A fresh database's baseline is built from the current models, so the
        old default itself can't be recreated here; production's column is
        altered by the same statement.)"""
        import os
        import subprocess

        import sqlalchemy as sa
        root = Path(__file__).resolve().parent.parent
        db = tmp_path / "mig.db"
        env = {**os.environ, "TX_DATABASE_URL": f"sqlite:///{db}"}

        def alembic(*args):
            r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env,
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stderr[-2000:]

        alembic("upgrade", "0044")
        eng = sa.create_engine(f"sqlite:///{db}")
        insert = ("INSERT INTO users (id, username, auth_source, created_at{col}) "
                  "VALUES (:id, :id, 'local', '2026-10-01'{val})")
        with eng.begin() as c:
            c.execute(sa.text(insert.format(col=", share_with_admins", val=", 1")),
                      {"id": "old-sharer"})
            c.execute(sa.text(insert.format(col=", share_with_admins", val=", 0")),
                      {"id": "old-private"})
        alembic("upgrade", "head")
        with eng.begin() as c:
            c.execute(sa.text(insert.format(col="", val="")), {"id": "new"})
            got = dict(c.execute(sa.text("SELECT id, share_with_admins FROM users")).fetchall())
        assert got == {"old-sharer": 1, "old-private": 0, "new": 0}
        alembic("downgrade", "0044")


# ------------------------------------------------- D-220: a local account needs an email
if DEPS:
    def _nomail_user(name, auth_source="local"):
        with SessionLocal() as s:
            s.query(User).filter_by(username=name).delete()
            s.add(User(username=name, password_hash=authn.hash_password("pw"),
                       auth_source=auth_source))
            s.commit()

    def _signed_in(name):
        c = TestClient(app)
        assert c.post("/login", data={"username": name, "password": "pw", "next": "/"},
                      follow_redirects=False).status_code == 303
        return c

    def _drop(*names):
        with SessionLocal() as s:
            s.query(User).filter(User.username.in_(names)).delete(synchronize_session=False)
            s.commit()

    @pytest.mark.email_gate
    def test_a_local_account_without_email_is_asked_at_every_visit_until_it_adds_one():
        _nomail_user("as-nomail")
        settings.auth_required = True
        try:
            c = _signed_in("as-nomail")
            r = c.get("/trips", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"].startswith("/account/email-required?next=/trips")
            assert "Add your email address" in c.get(r.headers["location"]).text
            # not saved: signing in again asks again
            c = _signed_in("as-nomail")
            assert c.get("/", follow_redirects=False).headers["location"].startswith(
                "/account/email-required")
            # the app isn't redirected, but is told to ask
            with SessionLocal() as s:
                tok = authn.issue_access_token(s.query(User).filter_by(username="as-nomail").one())
            api = {"Authorization": f"Bearer {tok}"}
            assert client.get("/api/v1/trips", headers=api).status_code == 200
            assert client.get("/api/v1/auth/me", headers=api).json()["email_required"] is True
            # the password is asked again, then the address is kept and the gate lifts
            r = c.post("/account/email-required", follow_redirects=False, data={
                "email": "nomail@example.com", "current_password": "wrong", "next": "/trips"})
            assert "err=" in r.headers["location"]
            r = c.post("/account/email-required", follow_redirects=False, data={
                "email": "nomail@example.com", "current_password": "pw", "next": "/trips"})
            assert r.status_code == 303 and r.headers["location"] == "/trips"
            assert c.get("/trips", follow_redirects=False).status_code == 200
            assert client.get("/api/v1/auth/me", headers=api).json()["email_required"] is False
        finally:
            settings.auth_required = False
            _drop("as-nomail")

    @pytest.mark.email_gate
    def test_sso_proxy_and_demo_accounts_are_not_asked_for_an_email(monkeypatch):
        with SessionLocal() as s:
            _drop("as-sso-nm", "as-proxy-nm", "as-demo-nm")
            s.add_all([User(username="as-sso-nm", auth_source="oidc"),
                       User(username="as-proxy-nm", auth_source="proxy"),
                       User(username="as-demo-nm", auth_source="local")])
            s.commit()
            monkeypatch.setattr(settings, "demo_mode", True)
            monkeypatch.setattr(settings, "demo_username", "as-demo-nm")
            try:
                for name in ("as-sso-nm", "as-proxy-nm", "as-demo-nm"):
                    assert authn.needs_email(s.query(User).filter_by(username=name).one()) is False
            finally:
                _drop("as-sso-nm", "as-proxy-nm", "as-demo-nm")
