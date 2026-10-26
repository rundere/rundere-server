"""D-211 (YONDER-336): admins have no standing access to people's data; a person
can let them look, read-only, for a while. Web and JSON API here; MCP is in
test_mcp_http.py. Everyone and everything is invented. SQLite via conftest."""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

try:
    from fastapi.testclient import TestClient
    from app import authn, scope, support
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (AccountEvent, Base, IngestBatch, JournalEntry, TravelItem, Trip,
                            TripActivity, User)
    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)
except ImportError:                                     # bare host: the container/CI runs these
    pytest.skip("fastapi not installed", allow_module_level=True)


def _wipe(s):
    uids = [u.id for u in s.query(User).filter(User.username.like("sa-%"))]
    tids = [t.id for t in s.query(Trip).filter(Trip.name.like("SA %"))]
    s.query(TripActivity).filter(TripActivity.trip_id.in_(tids)).delete(synchronize_session=False)
    s.query(Trip).filter(Trip.id.in_(tids)).delete(synchronize_session=False)
    s.query(TravelItem).filter(TravelItem.canonical_key.like("sa:%")).delete(
        synchronize_session=False)
    s.query(IngestBatch).filter(IngestBatch.api_key_id == "k-sa").delete(synchronize_session=False)
    s.query(JournalEntry).filter(JournalEntry.body.like("sa-%")).delete(synchronize_session=False)
    s.query(AccountEvent).filter(AccountEvent.user_id.in_(uids)).delete(synchronize_session=False)
    s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
    s.commit()


@pytest.fixture
def world(monkeypatch):
    """sa-admin, and sa-sis who lets admins look (a standing grant) with a trip,
    a capture, a received batch and a journal entry."""
    monkeypatch.setattr(settings, "auth_required", True)
    with SessionLocal() as s:
        _wipe(s)
        admin = User(username="sa-admin", password_hash=authn.hash_password("pw"),
                     auth_source="local", is_admin=True, email="sa-admin@example.test")
        sis = User(username="sa-sis", password_hash=authn.hash_password("pw"),
                   auth_source="local", share_with_admins=True, email="sa-sis@example.test")
        s.add_all([admin, sis]); s.flush()
        trip = Trip(name="SA Her Trip", source="manual", owner_id=sis.id)
        s.add(trip); s.flush()
        act = TripActivity(trip_id=trip.id, type="note", starts_at="",
                           values={"title": "SA note"})
        cap = TravelItem(owner_id=sis.id, canonical_key="sa:cap", source={}, status={})
        batch = IngestBatch(api_key_id="k-sa", user_id=sis.id, payload={}, status="received")
        note = JournalEntry(owner_id=sis.id, entry_date="2031-05-14", body="sa-her-diary",
                            source="web")
        s.add_all([act, cap, batch, note]); s.commit()
        ids = {"admin": admin.id, "sis": sis.id, "trip": trip.id, "act": act.id,
               "cap": cap.id, "batch": batch.id, "note": note.id}
    yield ids
    with SessionLocal() as s:
        _wipe(s)


def _h(uid):
    with SessionLocal() as s:
        return {"Authorization": f"Bearer {authn.issue_access_token(s.get(User, uid))}"}


def _set(uid, **fields):
    with SessionLocal() as s:
        u = s.get(User, uid)
        for k, v in fields.items():
            setattr(u, k, v)
        s.commit()


def _events(uid, action):
    with SessionLocal() as s:
        return s.query(AccountEvent).filter_by(user_id=uid, action=action).all()


# ------------------------------------------------------------------ read-only, web and API
def test_support_access_lets_an_admin_view_trips_and_inbox_but_not_the_journal(world):
    h = _h(world["admin"])
    assert client.get(f"/trips/{world['trip']}", headers=h).status_code == 200
    assert client.get(f"/review/item/{world['cap']}", headers=h).status_code == 200
    r = client.get(f"/api/v1/trips/{world['trip']}", headers=h)
    assert r.status_code == 200 and r.json()["trip"]["my_role"] == "viewer"
    assert client.get(f"/api/v1/review/{world['cap']}", headers=h).status_code == 200
    assert "sa-her-diary" not in client.get("/journal", headers=h).text
    assert client.get(f"/api/v1/journal/{world['note']}", headers=h).status_code == 404


def test_support_access_refuses_every_write(world):
    h = _h(world["admin"])
    # web: trip edit, activity delete, capture actions
    assert client.post(f"/trips/{world['trip']}", headers=h,
                       data={"name": "hijacked"}).status_code == 403
    assert client.post(f"/trips/{world['trip']}/activity/{world['act']}/delete",
                       headers=h).status_code == 403
    assert client.post(f"/review/item/{world['cap']}/ignore", headers=h).status_code == 403
    # API: trip, activity, capture approve/patch/ignore, filing
    assert client.patch(f"/api/v1/trips/{world['trip']}", headers=h,
                        json={"name": "hijacked"}).status_code == 403
    assert client.delete(f"/api/v1/trips/{world['trip']}/activities/{world['act']}",
                         headers=h).status_code == 403
    for verb in ("approve", "ignore", "unapprove"):
        assert client.post(f"/api/v1/review/{world['cap']}/{verb}", headers=h).status_code == 403
    assert client.patch(f"/api/v1/review/{world['cap']}", headers=h,
                        json={"payload": {}}).status_code in (403, 422)
    with SessionLocal() as s:
        assert s.get(Trip, world["trip"]).name == "SA Her Trip"
        assert s.get(TripActivity, world["act"]) is not None
        assert (s.get(TravelItem, world["cap"]).status or {}).get("review") is None


def test_an_admins_bulk_actions_never_touch_a_supported_persons_captures(world):
    """Process, Reprocess and Commit run over what the admin may CHANGE (D-211)."""
    with SessionLocal() as s:
        admin = s.get(User, world["admin"])
        mine = {i.id for i in s.query(TravelItem).filter(scope.capture_edit_filter(admin))}
        batches = {b.id for b in s.query(IngestBatch).filter(scope.batch_edit_filter(admin))}
        seen = {i.id for i in s.query(TravelItem).filter(scope.capture_filter(admin))}
    assert world["cap"] in seen and world["cap"] not in mine
    assert world["batch"] not in batches


def test_without_a_grant_an_admin_sees_nothing_of_hers(world):
    _set(world["sis"], share_with_admins=False)
    h = _h(world["admin"])
    assert client.get(f"/trips/{world['trip']}", headers=h,
                      follow_redirects=False).status_code == 404
    assert client.get(f"/api/v1/trips/{world['trip']}", headers=h).status_code == 404
    assert client.get(f"/api/v1/review/{world['cap']}", headers=h).status_code == 404


def test_a_time_limited_grant_stops_on_its_own_and_the_sweep_records_it(world):
    _set(world["sis"], support_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    h = _h(world["admin"])
    assert client.get(f"/api/v1/trips/{world['trip']}", headers=h).status_code == 404
    with SessionLocal() as s:
        assert support.expire_due(s) == 1
        u = s.get(User, world["sis"])
        assert u.share_with_admins is False and u.support_until is None
    assert len(_events(world["sis"], "support_expired")) == 1
    with SessionLocal() as s:
        assert support.expire_due(s) == 0                      # safe to repeat


# ------------------------------------------------------------------ granting, logging, telling
def test_granting_for_24_hours_over_the_api_is_audited_and_reported(world, monkeypatch):
    _set(world["sis"], share_with_admins=False)
    sent = []
    from app import email as mail
    monkeypatch.setattr(mail, "send_email", lambda to, subj, body: sent.append((to, body)) or True)
    with SessionLocal() as s:
        tok = authn.issue_access_token(s.get(User, world["sis"]))
    r = client.put("/api/v1/auth/me/sharing", headers={"Authorization": f"Bearer {tok}"},
                   json={"share": True, "duration": "24h"})
    assert r.status_code == 200, r.text
    until = datetime.fromisoformat(r.json()["support_until"])
    assert timedelta(hours=23) < until - datetime.now(timezone.utc) <= timedelta(hours=24)
    assert sent and sent[0][0] == "sa-sis@example.test" and "read-only" in sent[0][1]
    assert _events(world["sis"], "sharing_on")[0].detail == "for 24 hours"
    bad = client.put("/api/v1/auth/me/sharing", headers={"Authorization": f"Bearer {tok}"},
                     json={"share": True, "duration": "forever"})
    assert bad.status_code == 400


def test_the_web_grant_takes_a_duration_and_off_turns_it_off(world):
    _set(world["sis"], share_with_admins=False)
    h = _h(world["sis"])
    client.post("/account/sharing", headers=h, data={"choice": "share", "duration": "7d"})
    with SessionLocal() as s:
        u = s.get(User, world["sis"])
        assert u.share_with_admins and u.support_until is not None
    assert "Turn it off" in client.get("/account", headers=h).text
    client.post("/account/sharing", headers=h, data={"choice": "private"})
    with SessionLocal() as s:
        assert s.get(User, world["sis"]).share_with_admins is False


def test_looking_is_logged_once_a_day_and_shown_to_her(world):
    h = _h(world["admin"])
    for _ in range(3):
        client.get(f"/trips/{world['trip']}", headers=h)
        client.get(f"/api/v1/review/{world['cap']}", headers=h)
    views = _events(world["sis"], "support_viewed")
    assert len(views) == 1 and views[0].actor_id == world["admin"]
    page = client.get("/account", headers=_h(world["sis"])).text
    assert "Who looked" in page and "sa-admin" in page


def test_an_admin_on_her_roster_is_not_logged_as_looking(world):
    """Only support access is logged: being invited in is ordinary access."""
    from app.models import TripMember
    with SessionLocal() as s:
        s.add(TripMember(trip_id=world["trip"], user_id=world["admin"])); s.commit()
    client.get(f"/trips/{world['trip']}", headers=_h(world["admin"]))
    assert _events(world["sis"], "support_viewed") == []


def test_existing_sharers_see_a_notice_once_and_are_emailed_once(world, monkeypatch):
    _set(world["sis"], support_notice=True)
    h = _h(world["sis"])
    assert "Admins can now only look" in client.get("/trips", headers=h).text
    sent = []
    from app import email as mail
    monkeypatch.setattr(mail, "email_configured", lambda: True)
    monkeypatch.setattr(mail, "send_email", lambda to, subj, body: sent.append(to) or True)
    with SessionLocal() as s:
        assert support.send_notices(s) == 1
        assert support.send_notices(s) == 0                    # once
    assert sent == ["sa-sis@example.test"]
    client.post("/account/support-notice", headers=h)
    assert "Admins can now only look" not in client.get("/trips", headers=h).text


def test_an_admin_never_sets_someone_elses_password_even_under_support(world):
    r = client.post(f"/users/{world['sis']}/password", headers=_h(world["admin"]),
                    data={"password": "hijack"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    with SessionLocal() as s:
        assert authn.verify_password("pw", s.get(User, world["sis"]).password_hash)


# ------------------------------------------------------------------ the migration (0046)
def test_migration_0046_turns_existing_sharing_into_a_standing_grant_with_a_notice(tmp_path):
    import sqlalchemy as sa
    db = tmp_path / "mig.db"
    env = {**os.environ, "TX_DATABASE_URL": f"sqlite:///{db}"}

    def alembic(*args):
        r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-2000:]

    alembic("upgrade", "0045")
    eng = sa.create_engine(f"sqlite:///{db}")
    with eng.begin() as c:
        for uid, share in (("sharer", 1), ("private", 0)):
            c.execute(sa.text("INSERT INTO users (id, username, auth_source, created_at, "
                              "share_with_admins) VALUES (:id, :id, 'local', '2026-10-01', :s)"),
                      {"id": uid, "s": share})
    alembic("upgrade", "head")
    with eng.begin() as c:
        rows = {r[0]: (r[1], r[2], r[3]) for r in c.execute(sa.text(
            "SELECT id, share_with_admins, support_until, support_notice FROM users"))}
    assert rows == {"sharer": (1, None, 1), "private": (0, None, 0)}
    alembic("downgrade", "0045")
