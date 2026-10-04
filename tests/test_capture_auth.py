"""YONDER-210: with sign-in required, POST /capture/web authenticates itself.

/capture is a public path in the auth gate, so the route must refuse a stranger
before it fetches, parses or stores anything — otherwise anyone could make the
server fetch URLs and fill the database with ownerless captures (YONDER-186).
Sign-in off (the single-user default) is unchanged. SQLite via conftest."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

try:
    from fastapi.testclient import TestClient
    from app import authn, capture
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, IngestBatch, SavedPlace, User
    Base.metadata.create_all(engine)
    client = TestClient(app, follow_redirects=False)
except ImportError:
    pytest.skip("fastapi not installed", allow_module_level=True)

from extract import maps_url

PAGE = "<!doctype html><html><body><h1>A booking</h1></body></html>"
G_PLACE = "https://www.google.com/maps/place/X/@1.5,2.5,17z/data=!3d1.5!4d2.5"
G_DIR = "https://www.google.com/maps/dir/Toronto,+ON/Ottawa,+ON/@44.7,-77.0,8z"
SHORT = "https://maps.app.goo.gl/AbCdEf"


@pytest.fixture
def spy(monkeypatch):
    """Record every outbound fetch/expansion; none may happen for a stranger."""
    calls = {"fetch": 0, "expand": 0}

    def fetch(u):
        calls["fetch"] += 1
        return PAGE

    def expand(u):
        calls["expand"] += 1
        return u
    monkeypatch.setattr(capture, "_fetch_public_html", fetch)
    monkeypatch.setattr(maps_url, "_expand_short", expand)
    return calls


def _counts():
    with SessionLocal() as s:
        return s.query(IngestBatch).count(), s.query(SavedPlace).count()


@pytest.fixture
def signin_required(monkeypatch):
    monkeypatch.setattr(capture.settings, "auth_required", True)


@pytest.fixture
def user():
    with SessionLocal() as s:
        u = s.query(User).filter_by(username="ca_a").one_or_none()
        if u is None:
            u = User(username="ca_a", password_hash=authn.hash_password("pw"),
                     auth_source="local")
            s.add(u)
            s.commit()
        s.refresh(u)
        s.expunge(u)
    yield u
    with SessionLocal() as s:
        s.query(IngestBatch).filter(IngestBatch.user_id == u.id).delete()
        s.query(SavedPlace).filter(SavedPlace.owner_id == u.id).delete()
        s.commit()


FORMS = [
    ("an arbitrary URL", {"content": "https://example.com/some/page"}),
    ("pasted HTML", {"content": PAGE}),
    ("a directions link", {"content": G_DIR, "travel_date": "2026-09-23"}),
    ("an explicit directions path", {"content": G_DIR, "path": "google",
                                     "travel_date": "2026-09-23"}),
    ("a Maps place link", {"content": G_PLACE}),
    ("a Google short link", {"content": SHORT, "travel_date": "2026-09-23"}),
]


@pytest.mark.parametrize("what,data", FORMS, ids=[w for w, _ in FORMS])
def test_stranger_is_refused_before_anything_happens(signin_required, spy, what, data):
    before = _counts()
    r = client.post("/capture/web", data=data)
    assert r.status_code == 401 and "Sign in to capture" in r.text
    assert r.headers["HX-Redirect"] == "/login?next=/capture"   # htmx would not swap a bare 4xx
    assert _counts() == before and spy == {"fetch": 0, "expand": 0}


def test_the_legacy_raw_html_door_is_refused_too(signin_required, spy):
    before = _counts()
    r = client.post("/capture/web", content=PAGE.encode(), headers={"Content-Type": "text/html"})
    assert r.status_code == 401 and "sign in" in r.text.lower()
    assert _counts() == before and spy["fetch"] == 0


def test_a_bad_or_foreign_credential_is_still_a_stranger(signin_required, spy):
    before = _counts()
    for headers in ({"Authorization": "Bearer not-a-real-token"},
                    {"Cookie": f"{capture.settings.auth_cookie_name}=garbage"}):
        r = client.post("/capture/web", data={"content": PAGE}, headers=headers)
        assert r.status_code == 401
    assert _counts() == before


def test_a_signed_in_user_still_captures_and_owns_it(signin_required, spy, user):
    h = {"Authorization": f"Bearer {authn.issue_access_token(user)}"}
    r = client.post("/capture/web", data={"content": PAGE}, headers=h)
    assert r.status_code == 200 and "cap-msg err" not in r.text
    r = client.post("/capture/web", data={"content": G_PLACE}, headers=h)
    assert r.status_code == 200 and "Saved" in r.text
    with SessionLocal() as s:
        owners = {b.user_id for b in s.query(IngestBatch).filter(IngestBatch.user_id == user.id)}
        assert owners == {user.id}                       # never an ownerless batch
        assert s.query(SavedPlace).filter(SavedPlace.owner_id == user.id).count() == 1


def test_with_sign_in_off_nothing_changes(spy):
    """The single-user default stays open by design (SPEC-4 phase 2)."""
    assert capture.settings.auth_required is False
    before = _counts()
    r = client.post("/capture/web", data={"content": PAGE})
    assert r.status_code == 200 and _counts()[0] == before[0] + 1
    r = client.post("/capture/web", data={"content": G_PLACE})
    assert r.status_code == 200 and "Saved" in r.text
    with SessionLocal() as s:
        s.query(SavedPlace).filter(SavedPlace.owner_id.is_(None)).delete()
        s.commit()
