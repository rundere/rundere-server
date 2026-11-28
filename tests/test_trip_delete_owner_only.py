"""Only a trip's owner deletes it from the web (an admin for an ownerless trip,
D-211): a traveller edits the trip but can't remove it for everyone. The same
rule as the API's DELETE /api/v1/trips/{id} (YONDER-409).
Fictional people; SQLite via conftest.
"""
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_packing_engine_5 import DEPS, _h, pytestmark_app, world  # noqa: E402,F401

if DEPS:
    from tests.test_packing_engine_5 import client
    from app.db import SessionLocal
    from app.models import Trip, TripMember, User


def _exists(tid):
    with SessionLocal() as s:
        return s.get(Trip, tid) is not None


@pytestmark_app
def test_a_traveller_sees_no_delete_and_is_refused(world):
    tid = world["trip"]
    page = client.get(f"/trips/{tid}", headers=_h(world["b"]))
    assert page.status_code == 200 and "Edit trip" in page.text
    assert f"/trips/{tid}/delete" not in page.text
    r = client.post(f"/trips/{tid}/delete", headers=_h(world["b"]))
    assert r.status_code == 403 and _exists(tid)


@pytestmark_app
def test_a_viewer_gets_403_and_a_stranger_nothing(world):
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as s:
        vera = User(username=f"del-vera-{tag}", auth_source="local")
        sam = User(username=f"del-sam-{tag}", auth_source="local")
        s.add_all([vera, sam])
        s.flush()
        s.add(TripMember(trip_id=world["trip"], user_id=vera.id))
        s.commit()
        vid, sid = vera.id, sam.id
    try:
        # A stranger is sent back to their trips (HX-Redirect), as for any
        # trip they can't see, so existence never leaks.
        for uid, code in ((vid, 403), (sid, 200)):
            r = client.post(f"/trips/{world['trip']}/delete", headers=_h(uid),
                            follow_redirects=False)
            assert r.status_code == code
        assert _exists(world["trip"])
    finally:
        with SessionLocal() as s:
            s.query(TripMember).filter_by(trip_id=world["trip"]).delete()
            s.query(User).filter(User.id.in_([vid, sid])).delete(synchronize_session=False)
            s.commit()


@pytestmark_app
def test_the_owner_sees_delete_and_deletes(world):
    tid = world["trip"]
    page = client.get(f"/trips/{tid}", headers=_h(world["a"]))
    assert f"/trips/{tid}/delete" in page.text
    r = client.post(f"/trips/{tid}/delete", headers=_h(world["a"]), follow_redirects=False)
    assert r.status_code in (200, 303) and not _exists(tid)


@pytestmark_app
def test_an_admin_deletes_an_ownerless_trip_but_not_an_owned_one(world):
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as s:
        root = User(username=f"del-root-{tag}", auth_source="local", is_admin=True)
        open_trip = Trip(name="Example open weekend", source="manual",
                         start_date="2031-07-01", end_date="2031-07-03")
        s.add_all([root, open_trip])
        s.commit()
        rid, oid = root.id, open_trip.id
    try:
        # An owned trip isn't the admin's to see (D-211): nothing happens.
        client.post(f"/trips/{world['trip']}/delete", headers=_h(rid),
                    follow_redirects=False)
        assert _exists(world["trip"])
        client.post(f"/trips/{oid}/delete", headers=_h(rid), follow_redirects=False)
        assert not _exists(oid)
    finally:
        with SessionLocal() as s:
            s.query(Trip).filter(Trip.id == oid).delete()
            s.query(User).filter(User.id == rid).delete()
            s.commit()
