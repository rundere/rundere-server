"""The pre-release cosmetic batch: readable time zones (YONDER-321), a country
picker for nationality (320), no AirTrail id on the Travel profile (322), a Home profiles
primer (323) and a second Save on the packing profile (327). The carry-on
weight (325) is tested in test_planning_engine.py and the email-forward
guide (329) in test_inbox.py. People and places here are invented."""
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

try:
    from fastapi.testclient import TestClient
    from app import authn, clock
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, TravelerIdentity, User
    Base.metadata.create_all(engine)
    client = TestClient(app)
except ImportError:                                  # bare host: the container/CI runs these
    pytest.skip("fastapi not installed", allow_module_level=True)


@pytest.fixture()
def person(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as s:
        t = TravelerIdentity(display_name=f"Ui Tess {tag}", kind="guest")
        s.add(t)
        s.flush()
        u = User(username=f"ui-{tag}", auth_source="local", traveler_id=t.id)
        s.add(u)
        s.commit()
        s.refresh(u)
        s.expunge(u)
    yield u
    with SessionLocal() as s:
        s.query(User).filter(User.id == u.id).delete()
        s.query(TravelerIdentity).filter(TravelerIdentity.id == u.traveler_id).delete()
        s.commit()


def _ck(u):
    return {settings.auth_cookie_name: authn.issue_access_token(u)}


def _trav(u):
    with SessionLocal() as s:
        return s.get(TravelerIdentity, u.traveler_id)


# ------------------------------------------------------------- time zones (321)
def test_zone_labels_are_readable_and_ordered_by_offset():
    opts = clock.zone_options()
    assert {z for z, _ in opts} == set(clock.zone_names())        # same values, all of them
    label = dict(opts)["Asia/Kolkata"]
    assert label.startswith("(UTC+05:30) Kolkata") and "India" in label and "IST" in label
    offsets = [lbl.split(")", 1)[0] for _, lbl in opts]
    assert offsets[0].startswith("(UTC−") and offsets[-1].startswith("(UTC+")


def test_travel_profile_shows_readable_zones_and_still_saves_the_iana_name(person):
    r = client.post("/trips/settings/travel-profile", cookies=_ck(person),
                    data={"timezone": "Asia/Kolkata"})
    assert 'value="Asia/Kolkata" selected>(UTC+05:30) Kolkata' in r.text
    assert _trav(person).timezone == "Asia/Kolkata"


# ------------------------------------------------------------ nationality (320)
def test_nationality_is_a_country_picker_saving_the_iso_code(person):
    html = client.get("/trips/settings/travel-profile", cookies=_ck(person)).text
    assert '<select id="tp-nat"' in html and '<option value="IN" >India</option>' in html
    assert "European Union" not in html                           # not a nationality
    client.post("/trips/settings/travel-profile", cookies=_ck(person), data={"nationality": "IN"})
    assert _trav(person).nationality == "IN"
    client.post("/trips/settings/travel-profile", cookies=_ck(person), data={"nationality": "QQ"})
    assert _trav(person).nationality is None                      # not a country: refused


def test_a_code_saved_before_the_picker_is_kept(person):
    with SessionLocal() as s:
        s.get(TravelerIdentity, person.traveler_id).nationality = "YU"   # not in today's list
        s.commit()
    html = client.get("/trips/settings/travel-profile", cookies=_ck(person)).text
    assert '<option value="YU" selected>YU</option>' in html
    client.post("/trips/settings/travel-profile", cookies=_ck(person), data={"nationality": "YU"})
    assert _trav(person).nationality == "YU"


# ---------------------------------------------------------- AirTrail id (322)
def test_the_travel_profile_no_longer_asks_for_an_airtrail_id(person):
    """Whose seat an AirTrail flight is comes from the AirTrail connection
    (blank: the token's own user). Saving the profile leaves an old value alone:
    it's only the one-time seed when server-wide settings become a connection."""
    html = client.get("/trips/settings/travel-profile", cookies=_ck(person)).text
    assert 'name="airtrail_user_id"' not in html and "AirTrail user id" not in html
    with SessionLocal() as s:
        s.get(TravelerIdentity, person.traveler_id).airtrail_user_id = "at-4471"
        s.commit()
    client.post("/trips/settings/travel-profile", cookies=_ck(person),
                data={"nationality": "IN", "airtrail_user_id": "ignored"})
    assert _trav(person).airtrail_user_id == "at-4471"


def test_the_seat_comes_from_the_connection_never_the_travel_profile(person):
    from app import integrations
    from app.models import UserIntegration
    with SessionLocal() as s:
        s.get(TravelerIdentity, person.traveler_id).airtrail_user_id = "at-old"
        s.add(UserIntegration(user_id=person.id, kind="airtrail", enabled=True,
                              base_url="https://airtrail.example.test"))
        s.commit()
    try:
        with SessionLocal() as s:
            c = integrations.clients_for_user(s, person.id)
            assert c.airtrail_user_id is None          # token's own user, not "at-old"
            row = s.query(UserIntegration).filter_by(user_id=person.id).one()
            row.remote_user_id = "at-conn"
            s.commit()
            assert integrations.clients_for_user(s, person.id).airtrail_user_id == "at-conn"
    finally:
        with SessionLocal() as s:
            s.query(UserIntegration).filter_by(user_id=person.id).delete()
            s.commit()


# --------------------------------------------------------- home primer (323)
def test_home_profiles_say_what_each_home_is_for(person):
    html = client.get("/review/home", cookies=_ck(person)).text
    assert "Why Run·dere asks where home is" in html
    assert "starts a trip" in html and "isn't a trip" in html


# ------------------------------------------------ packing profile Save (327)
def test_packing_profile_can_be_saved_above_the_must_haves(person):
    html = client.get("/trips/settings/packing-profile", cookies=_ck(person)).text
    assert html.count(">Save profile</button>") == 2
    assert html.index(">Save profile</button>") < html.index("Always pack these")
    assert "Carry-on weight limit (kg)" in html and "checked bags aren't counted" in html
