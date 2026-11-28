"""5.1, YONDER-411 part 2: which of your bags and templates a trip's list uses,
from the apps (PUT /api/v1/trips/{id}/packing/bags and /templates).

The same rules as the web's packing section (SPEC-5 §6.7.3, §6.10.1): your own
list (D-123), your own bags and templates (§6.6), edit access to the trip.
Fictional people; SQLite via conftest.
"""
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest                                               # noqa: E402

from tests.test_packing_engine_5 import (DEPS, _answer, _h,  # noqa: E402,F401
                                         pytestmark_app, world)

if DEPS:
    from tests.test_packing_engine_5 import client
    from app.db import SessionLocal
    from app.models import (Bag, PackingItem, PackingPlan, PackingTemplate, TripMember,
                            User)

T = "/api/v1/trips"


def _bags(ids, who, *specs):
    with SessionLocal() as s:
        rows = [Bag(user_id=ids[who], label=label, kind=kind, capacity=cap)
                for label, kind, cap in specs]
        s.add_all(rows)
        s.commit()
        return [r.id for r in rows]


def _template(ids, who, type_key, name):
    with SessionLocal() as s:
        t = PackingTemplate(user_id=ids[who], type_key=type_key, name=name)
        s.add(t)
        s.commit()
        return t.id


def _plan(ids, who="a"):
    with SessionLocal() as s:
        return (s.query(PackingPlan).filter_by(trip_id=ids["trip"], user_id=ids[who])
                .order_by(PackingPlan.generated_at.desc()).first())


@pytestmark_app
def test_picking_bags_generates_a_list_and_packs_it_into_them(world):
    _answer(world, "a")
    big, small = _bags(world, "a", ("Example suitcase", "suitcase", "60L"),
                       ("Example daypack", "backpack", "20L"))
    r = client.put(f"{T}/{world['trip']}/packing/bags", headers=_h(world["a"]),
                   json={"bag_ids": [big, small]})
    assert r.status_code == 200, r.text
    assert r.json()["bag_ids"] == [big, small] and r.json()["items"]
    assert _plan(world).bag_ids == [big, small]
    with SessionLocal() as s:
        rows = s.query(PackingItem).filter_by(trip_id=world["trip"], user_id=world["a"],
                                              status="draft").all()
        assert rows and {r.bag for r in rows} <= {big, small}
    r = client.put(f"{T}/{world['trip']}/packing/bags", headers=_h(world["a"]),
                   json={"bag_ids": []})
    assert r.json()["bag_ids"] == [] and not _plan(world).bag_ids


@pytestmark_app
def test_templates_set_the_list_and_none_goes_back_to_the_suggestion(world):
    _answer(world, "a")
    tpl = _template(world, "a", "hiking", "Example hills")
    r = client.put(f"{T}/{world['trip']}/packing/templates", headers=_h(world["a"]),
                   json={"template_ids": [tpl, tpl]})
    assert r.status_code == 200, r.text
    assert [a.get("id") for a in r.json()["archetypes"]] == [tpl]
    r = client.put(f"{T}/{world['trip']}/packing/templates", headers=_h(world["a"]),
                   json={"template_ids": []})
    assert r.status_code == 200
    assert tpl not in [a.get("id") for a in (r.json()["archetypes"] or [])]


@pytestmark_app
def test_without_the_questionnaire_its_the_profile_409(world):
    r = client.put(f"{T}/{world['trip']}/packing/templates", headers=_h(world["a"]),
                   json={"template_ids": []})
    assert r.status_code == 409 and r.json()["code"] == "packing_profile_required"


@pytestmark_app
def test_someone_elses_bags_and_templates_are_refused(world):
    _answer(world, "a")
    _answer(world, "b")
    (bens_bag,) = _bags(world, "b", ("Ben's example pack", "backpack", "40L"))
    bens_tpl = _template(world, "b", "beach", "Ben's example beach")
    h = _h(world["a"])
    assert client.put(f"{T}/{world['trip']}/packing/bags", headers=h,
                      json={"bag_ids": [bens_bag]}).status_code == 422
    assert client.put(f"{T}/{world['trip']}/packing/templates", headers=h,
                      json={"template_ids": [bens_tpl]}).status_code == 422
    assert _plan(world) is None
    # Ben's own pick only ever touches Ben's list.
    client.put(f"{T}/{world['trip']}/packing/bags", headers=_h(world["b"]),
               json={"bag_ids": [bens_bag]})
    assert _plan(world, "b").bag_ids == [bens_bag] and _plan(world) is None


@pytestmark_app
def test_a_viewer_gets_403_and_a_stranger_404(world):
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as s:
        vera = User(username=f"tp-vera-{tag}", auth_source="local")
        sam = User(username=f"tp-sam-{tag}", auth_source="local")
        s.add_all([vera, sam])
        s.flush()
        s.add(TripMember(trip_id=world["trip"], user_id=vera.id))
        s.commit()
        vid, sid = vera.id, sam.id
    try:
        for uid, code in ((vid, 403), (sid, 404)):
            for path, body in (("bags", {"bag_ids": []}), ("templates", {"template_ids": []})):
                r = client.put(f"{T}/{world['trip']}/packing/{path}", headers=_h(uid), json=body)
                assert r.status_code == code, (path, r.status_code)
    finally:
        with SessionLocal() as s:
            s.query(TripMember).filter_by(trip_id=world["trip"]).delete()
            s.query(User).filter(User.id.in_([vid, sid])).delete(synchronize_session=False)
            s.commit()
