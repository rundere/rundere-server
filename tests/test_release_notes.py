"""What's new after an upgrade (SPEC-8 §14, D-234, YONDER-367): the bundled notes
file, what each person is shown and when, the web dialog, the About page and the
API the apps use. Everyone here is invented. SQLite via conftest."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import re

import pytest

try:
    from fastapi.testclient import TestClient
    from app import authn, release_notes as rn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, User
    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)
except ImportError:                                     # bare host: the container/CI runs these
    pytest.skip("fastapi not installed", allow_module_level=True)

SAMPLE = """# Notes
<!-- ## 9.9.9 — 2031-01-01 inside a comment is not a section -->

## 2.0.0

### For everyone
- Not out yet.

## 1.2.0 — 2031-03-01

### For everyone
- **Bold** news with `code` and a [link](https://example.test/notes).

### For admins
- Run `example-command` once.

## 1.1.0 — 2031-02-01

### For admins
- Admins only, this time.

## 1.0.10 — 2031-01-20

### For everyone
- Ten.

## 1.0.9 — 2031-01-10

### For everyone
- Nine.
"""


@pytest.fixture
def sample(monkeypatch, tmp_path):
    p = tmp_path / "release_notes.md"
    p.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr(rn, "PATH", p)
    rn._cached.cache_clear()
    yield p
    rn._cached.cache_clear()


class _U:
    def __init__(self, seen=None, admin=False, uid="u-1"):
        self.seen_version, self.is_admin, self.id = seen, admin, uid


# ------------------------------------------------------------------ the bundled file

def test_the_bundled_notes_cover_the_running_version_and_parse_cleanly():
    rn._cached.cache_clear()
    vs = rn.versions()
    names = [v.version for v in vs]
    assert settings.app_version in names, "add a section for this version to app/release_notes.md"
    assert len(names) == len(set(names)), "a version appears twice"
    assert names == sorted(names, key=rn.vkey, reverse=True)
    for v in vs:
        assert v.everyone or v.admins, f"{v.version} has no items"
        assert v.date is None or re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.date)
    # Every released version has a date; only one not out yet may lack it.
    assert all(v.date for v in rn.released())
    assert "1.2.0" in names and "4.0.0" in names           # the history goes back


def test_versions_sort_as_numbers():
    assert rn.vkey("4.12.10") > rn.vkey("4.12.9") > rn.vkey("4.9.0")
    assert rn.vkey("v3.8.1") == (3, 8, 1) and rn.vkey("junk") == ()


# ------------------------------------------------------------------ who sees what

def test_parse_skips_comments_and_reads_audiences(sample):
    vs = rn.versions()
    assert [v.version for v in vs] == ["2.0.0", "1.2.0", "1.1.0", "1.0.10", "1.0.9"]
    v = vs[1]
    assert v.date == "2031-03-01" and len(v.everyone) == 1 and v.admins == ["Run `example-command` once."]


def test_unseen_is_the_range_since_last_seen_newest_first(sample):
    got = rn.unseen(_U(seen="1.0.9"), running="1.2.0")
    assert [v["version"] for v in got] == ["1.2.0", "1.0.10"]      # 1.1.0 is admins-only
    assert got[0]["admins"] == []                                 # not an admin
    admin = rn.unseen(_U(seen="1.0.9", admin=True), running="1.2.0")
    assert [v["version"] for v in admin] == ["1.2.0", "1.1.0", "1.0.10"]
    assert admin[0]["admins"] == ["Run `example-command` once."]
    # A version not out yet stays hidden; seen up to date shows nothing.
    assert rn.unseen(_U(seen="1.2.0"), running="1.2.0") == []
    assert "2.0.0" not in [v["version"] for v in rn.unseen(_U(seen="1.0.9"), running="1.2.0")]


def test_an_account_from_before_shows_only_the_running_version(sample):
    assert [v["version"] for v in rn.unseen(_U(seen=None), running="1.2.0")] == ["1.2.0"]
    assert rn.unseen(None, running="1.2.0") == []


def test_at_most_five_versions(sample, monkeypatch):
    many = "\n".join(f"## 1.5.{i} — 2031-04-{i + 1:02d}\n\n### For everyone\n- Item {i}.\n"
                     for i in range(8))
    sample.write_text(many, encoding="utf-8")
    rn._cached.cache_clear()
    got = rn.unseen(_U(seen="1.0.0"), running="1.5.7")
    assert [v["version"] for v in got] == ["1.5.7", "1.5.6", "1.5.5", "1.5.4", "1.5.3"]


def test_render_allows_only_bold_code_and_https_links():
    out = str(rn.render("**Hi** `x<y` [ok](https://example.test/a) "
                        "[bad](javascript:alert(1)) <script>alert(1)</script>"))
    assert "<strong>Hi</strong>" in out and "<code>x&lt;y</code>" in out
    assert '<a href="https://example.test/a" target="_blank" rel="noopener">ok</a>' in out
    assert "javascript:" not in out.split("[bad]")[0] and "<script>" not in out
    assert "&lt;script&gt;" in out


# ------------------------------------------------------------------ web and API

def _wipe(s):
    s.query(User).filter(User.username.like("rn-%")).delete(synchronize_session=False)
    s.commit()


@pytest.fixture
def people(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    with SessionLocal() as s:
        _wipe(s)
        new = User(username="rn-new", password_hash=authn.hash_password("pw"),
                   auth_source="local", email="rn-new@example.test")
        old = User(username="rn-old", password_hash=authn.hash_password("pw"),
                   auth_source="local", email="rn-old@example.test")
        admin = User(username="rn-admin", password_hash=authn.hash_password("pw"),
                     auth_source="local", is_admin=True, email="rn-admin@example.test")
        s.add_all([new, old, admin]); s.flush()
        old.seen_version = None                  # an account from before 5.0
        admin.seen_version = None
        s.commit()
        ids = {"new": new.id, "old": old.id, "admin": admin.id}
    yield ids
    with SessionLocal() as s:
        _wipe(s)


def _h(uid):
    with SessionLocal() as s:
        return {"Authorization": f"Bearer {authn.issue_access_token(s.get(User, uid))}"}


def _seen(uid):
    with SessionLocal() as s:
        return s.get(User, uid).seen_version


def test_a_new_account_starts_at_the_running_version(people):
    assert _seen(people["new"]) == settings.app_version
    assert 'id="whats-new"' not in client.get("/trips", headers=_h(people["new"])).text


def test_the_dialog_shows_once_and_closing_it_records_it(people):
    h = _h(people["old"])
    page = client.get("/trips", headers=h).text
    assert 'id="whats-new"' in page and f">{settings.app_version}" in page
    assert "whats_new.js" in page
    # htmx partials never carry it; only full pages with the app header do.
    r = client.post("/whats-new/seen", headers={**h, "X-Requested-With": "fetch"})
    assert r.status_code == 204 and _seen(people["old"]) == settings.app_version
    assert 'id="whats-new"' not in client.get("/trips", headers=h).text


def test_the_no_script_form_stays_on_the_page(people):
    """No redirect at all: a 204 leaves the browser where it was, so nothing the
    browser sends decides where it goes."""
    r = client.post("/whats-new/seen", headers=_h(people["old"]),
                    data={"next": "https://evil.example.test/"}, follow_redirects=False)
    assert r.status_code == 204 and "location" not in r.headers
    assert _seen(people["old"]) == settings.app_version


def test_admins_see_their_items_others_dont(people, sample, monkeypatch):
    monkeypatch.setattr(settings, "app_version", "1.2.0")
    admin = client.get("/trips", headers=_h(people["admin"])).text
    assert "For admins" in admin and "example-command" in admin
    other = client.get("/trips", headers=_h(people["old"])).text
    assert 'id="whats-new"' in other and "example-command" not in other


def test_nothing_for_this_person_is_recorded_silently(people, sample, monkeypatch):
    monkeypatch.setattr(settings, "app_version", "1.1.0")      # admins-only notes
    with SessionLocal() as s:
        s.get(User, people["old"]).seen_version = "1.0.10"
        s.commit()
    assert 'id="whats-new"' not in client.get("/trips", headers=_h(people["old"])).text
    assert _seen(people["old"]) == "1.1.0"


def test_about_links_to_every_version(people):
    h = _h(people["new"])
    about = client.get("/about", headers=h).text
    assert 'href="/about/release-notes"' in about and "/releases" in about
    page = client.get("/about/release-notes", headers=h).text
    assert ">1.2.0" in page and f">{settings.app_version}" in page
    # Signed out it's behind sign-in, unlike /about itself.
    r = TestClient(app).get("/about/release-notes", follow_redirects=False)
    assert r.status_code in (302, 303, 401)


def test_the_api_lists_unseen_notes_and_shares_seen_with_the_web(people, sample, monkeypatch):
    monkeypatch.setattr(settings, "app_version", "1.2.0")
    h = _h(people["admin"])
    with SessionLocal() as s:
        s.get(User, people["admin"]).seen_version = "1.0.9"
        s.commit()
    body = client.get("/api/v1/release-notes", headers=h).json()
    assert body["running_version"] == "1.2.0" and body["seen_version"] == "1.0.9"
    assert [v["version"] for v in body["versions"]] == ["1.2.0", "1.1.0", "1.0.10"]
    every = client.get("/api/v1/release-notes?all=true", headers=h).json()["versions"]
    assert [v["version"] for v in every] == ["1.2.0", "1.1.0", "1.0.10", "1.0.9"]
    assert client.post("/api/v1/release-notes/seen", headers=h).status_code == 204
    assert client.get("/api/v1/release-notes", headers=h).json()["versions"] == []
    # Seen in the app means no dialog on the web.
    assert 'id="whats-new"' not in client.get("/trips", headers=h).text
    # A non-admin never gets admin items, even asking for everything.
    mine = client.get("/api/v1/release-notes?all=true", headers=_h(people["old"])).json()
    assert all(v["admins"] == [] for v in mine["versions"])
    assert "1.1.0" not in [v["version"] for v in mine["versions"]]


def test_the_api_routes_are_in_the_schema():
    paths = app.openapi()["paths"]
    assert "/api/v1/release-notes" in paths and "/api/v1/release-notes/seen" in paths
