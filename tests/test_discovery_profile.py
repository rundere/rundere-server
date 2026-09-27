"""Discovery Profile editor (Yonder 3.2, SPEC-3.2 §11.1).

Peer of the Packing Profile page: a structured form (checkboxes/selects, no JSON)
that persists a constraint-first preference blob to DiscoveryProfile.values.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, DiscoveryProfile
    Base.metadata.create_all(engine)
    _client = TestClient(app)
    DEPS = True
except ImportError:
    DEPS = False


def _reset():
    with SessionLocal() as s:
        s.query(DiscoveryProfile).delete()
        s.commit()


def test_form_is_structured_not_json():
    if not DEPS:
        return
    r = _client.get("/trips/settings/discovery-profile")
    assert r.status_code == 200
    # structured fields, not a JSON blob — the one textarea is the free-text notes (4.5)
    assert "<select" in r.text and r.text.count("<textarea") == 1
    assert 'name="notes"' in r.text
    for name in ("dietary_restrictions", "cuisine_dislikes", "allergies",
                 "budget_ceiling", "drink_interests", "activity_interests",
                 "mobility"):
        assert f'name="{name}"' in r.text


def test_nav_links_discovery_profile():
    if not DEPS:
        return
    r = _client.get("/trips/settings/discovery-profile")
    assert "/trips/settings/discovery-profile" in r.text
    assert "Discovery" in r.text and "profile" in r.text.lower()


def test_saves_constraint_first_blob():
    if not DEPS:
        return
    _reset()
    try:
        r = _client.post("/trips/settings/discovery-profile", data={
            "dietary_restrictions": ["vegetarian", "halal"],
            "cuisine_dislikes": " sushi ,  fast food ,",     # trimmed, empties dropped
            "allergies": "  peanuts  ",
            "budget_ceiling": "2",
            "drink_interests": ["craft_beer", "coffee"],
            "activity_interests": ["museums", "hidden_gems"],
            "mobility": "limited"})
        assert r.status_code == 200 and "saved" in r.text.lower()
        with SessionLocal() as s:
            v = s.query(DiscoveryProfile).one().values
            assert set(v["food"]["dietary_restrictions"]) == {"vegetarian", "halal"}
            assert v["food"]["cuisine_dislikes"] == ["sushi", "fast food"]
            assert v["food"]["allergies"] == "peanuts"
            assert v["food"]["budget_ceiling"] == 2          # int, not "2"
            assert set(v["drink"]["interests"]) == {"craft_beer", "coffee"}
            assert set(v["activity"]["interests"]) == {"museums", "hidden_gems"}
            assert v["practical"]["mobility"] == "limited"
    finally:
        _reset()


def test_blank_profile_is_all_defaults():
    if not DEPS:
        return
    _reset()
    try:
        r = _client.post("/trips/settings/discovery-profile", data={})
        assert r.status_code == 200
        with SessionLocal() as s:
            v = s.query(DiscoveryProfile).one().values
            assert v["food"] == {"dietary_restrictions": [], "cuisine_dislikes": [],
                                 "allergies": "", "budget_ceiling": 0}
            assert v["drink"]["interests"] == []
            assert v["activity"]["interests"] == []
            assert v["practical"]["mobility"] == "no_limits"
    finally:
        _reset()


def test_junk_values_are_filtered():
    if not DEPS:
        return
    _reset()
    try:
        _client.post("/trips/settings/discovery-profile", data={
            "budget_ceiling": "9",                           # out of range -> 0
            "drink_interests": ["craft_beer", "not_a_drink"],
            "activity_interests": ["museums", "bogus"],
            "mobility": "teleport"})                          # unknown -> no_limits
        with SessionLocal() as s:
            v = s.query(DiscoveryProfile).one().values
            assert v["food"]["budget_ceiling"] == 0
            assert v["drink"]["interests"] == ["craft_beer"]
            assert v["activity"]["interests"] == ["museums"]
            assert v["practical"]["mobility"] == "no_limits"
    finally:
        _reset()


def test_save_updates_existing_row_not_duplicate():
    if not DEPS:
        return
    _reset()
    try:
        _client.post("/trips/settings/discovery-profile",
                     data={"budget_ceiling": "1"})
        _client.post("/trips/settings/discovery-profile",
                     data={"budget_ceiling": "3"})
        with SessionLocal() as s:
            rows = s.query(DiscoveryProfile).all()
            assert len(rows) == 1                            # updated in place
            assert rows[0].values["food"]["budget_ceiling"] == 3
    finally:
        _reset()
