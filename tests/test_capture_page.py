"""SPEC-9 D-159/D-160: the capture page is the always-used box plus a click-through guide with a
tab per channel; the bookmarklet hands the page to a Run·dere tab; keys are per user and shown once."""
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
    from app import auth, authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import ApiKey, Base, IngestBatch, TravelItem, User

    Base.metadata.create_all(engine)
    client = TestClient(app)
    PAGE = "<!doctype html><html><body><h1>Handed over</h1><p>Booking</p></body></html>"

    def _user(name):
        with SessionLocal() as s:
            u = User(username=name, auth_source="local")
            s.add(u); s.commit()
            return u.id, {settings.auth_cookie_name: authn.issue_access_token(u)}

    def _cleanup(*names):
        with SessionLocal() as s:
            uids = [u.id for u in s.query(User).filter(User.username.in_(names)).all()]
            bids = [b.id for b in s.query(IngestBatch).filter(IngestBatch.user_id.in_(uids)).all()]
            s.query(TravelItem).filter(TravelItem.batch_id.in_(bids)).delete(synchronize_session=False)
            s.query(IngestBatch).filter(IngestBatch.id.in_(bids)).delete(synchronize_session=False)
            s.query(ApiKey).filter(ApiKey.user_id.in_(uids)).delete(synchronize_session=False)
            s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
            s.commit()

    def test_capture_page_is_the_box_and_a_link_not_the_guides():
        html = client.get("/capture").text
        assert "Capture something" in html and 'href="/capture/setup"' in html
        assert "script.google.com" not in html and "Apps Script" not in html   # guides live on /capture/setup
        assert 'name="content"' in html and "More options" in html

    def test_every_tab_is_an_instruction_manual():
        marks = {"gmail": ["script.google.com", "Install add-on", "DEFAULT_INGEST_URL", "Check it works"],
                 "calendar": ["calendar.addons.execute", "Install add-on", "Check it works"],
                 "bookmarklet": ["bookmarks bar", "/capture/handoff", "Check it works"],
                 "email": ["Not set up on this server yet", "trips+", "forward"],   # D-190; no mailbox in tests
                 "ios": ["Not available yet", "Share"],
                 "mcp": ["Model Context Protocol", "/mcp", "capture_url", "Check it works"]}
        for tab, needles in marks.items():
            html = client.get(f"/capture/setup?tab={tab}").text
            for n in needles:
                assert n in html, (tab, n)
            assert html.count('class="cap-tab') >= 6
        assert 'aria-selected="true"' in client.get("/capture/setup?tab=nope").text   # falls back to Gmail
        old = client.get("/capture/setup?tab=claude").text                            # the old tab name
        assert "Model Context Protocol" in old

    def test_gmail_guide_has_no_label_steps_and_no_modify_scope():
        html = client.get("/capture/setup?tab=gmail").text
        assert "LABEL_TRACKED" not in html and "getUserLabelByName" not in html   # the shipped Code.gs
        assert "gmail.modify" not in html.split("If it doesn")[0]

    def test_bookmarklet_hands_over_with_postmessage_not_fetch():
        from app.capture import _bookmarklet
        src = _bookmarklet("https://rd.example")
        assert "window.open(O+'/capture/handoff'" in src and "postMessage" in src
        assert "fetch(" not in src and "https://rd.example" in src
        assert "e.origin!==O" in src                      # only Run·dere's own reply is acted on

    def test_handoff_requires_sign_in_when_sign_in_is_on(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        r = client.get("/capture/handoff", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
        _, ck = _user("cp-hand")
        try:
            page = client.get("/capture/handoff", cookies=ck)
            assert page.status_code == 200 and "rundere-page" in page.text and "e.source !== window.opener" in page.text
        finally:
            _cleanup("cp-hand")

    def test_handed_over_page_keeps_its_source_url(monkeypatch):
        monkeypatch.setattr(settings, "auto_process", False)
        r = client.post("/capture/web", data={"content": PAGE, "path": "page",
                                              "source_url": "https://hotel.example/booking/9"})
        assert r.status_code == 200
        with SessionLocal() as s:
            b = s.query(IngestBatch).order_by(IngestBatch.received_at.desc()).first()
            assert b.payload["source_url"] == "https://hotel.example/booking/9"
            s.query(IngestBatch).filter(IngestBatch.id == b.id).delete(); s.commit()

    def test_key_is_per_user_hashed_shown_once_and_replaced_on_remake(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        uid, ck = _user("cp-key")
        try:
            r = client.post("/capture/setup/key", data={"channel": "gmail"}, cookies=ck)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
            key1 = r.text.split('id="key-gmail">')[1].split("<")[0]
            assert key1 and "/ingest" in r.text
            with SessionLocal() as s:
                row = s.query(ApiKey).filter(ApiKey.user_id == uid).one()
                assert row.client_name == "gmail-addon-cp-key" and row.key_hash == auth.hash_key(key1)
                assert key1 not in (row.key_hash, row.client_name)       # only the hash is stored
            key2 = client.post("/capture/setup/key", data={"channel": "gmail"}, cookies=ck
                               ).text.split('id="key-gmail">')[1].split("<")[0]
            with SessionLocal() as s:
                assert s.query(ApiKey).filter(ApiKey.user_id == uid).count() == 1
                assert auth.lookup_key(s, key1) is None and auth.lookup_key(s, key2) is not None
            # the guide page itself never contains a key
            assert key2 not in client.get("/capture/setup?tab=gmail", cookies=ck).text
            assert client.post("/capture/setup/key", data={"channel": "gmail"}).status_code == 401
        finally:
            _cleanup("cp-key")

    def test_recent_captures_and_tab_status_are_the_users_own(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        monkeypatch.setattr(settings, "auto_process", False)
        a_id, a = _user("cp-a")
        b_id, b = _user("cp-b")
        try:
            with SessionLocal() as s:
                s.add(IngestBatch(api_key_id="x", user_id=a_id, status="received",
                                  payload={"capture_channel": "gmail"}))
                s.commit()
            mine = client.get("/capture/setup?tab=gmail", cookies=a).text
            theirs = client.get("/capture/setup?tab=gmail", cookies=b).text
            assert 'class="cap-tab-st">just now' in mine and 'class="cap-tab-st"' not in theirs
            assert "Page waiting to be read" in client.get("/capture", cookies=a).text
            assert "Page waiting to be read" not in client.get("/capture", cookies=b).text
        finally:
            _cleanup("cp-a", "cp-b")

    def test_tab_status_separates_the_bookmarklet_from_the_capture_box(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        monkeypatch.setattr(settings, "auto_process", False)
        u_id, ck = _user("cp-via")
        try:
            with SessionLocal() as s:
                s.add(IngestBatch(api_key_id="x", user_id=u_id, status="received",
                                  payload={"capture_channel": "web", "html": PAGE}))
                s.commit()
            html = client.get("/capture/setup?tab=bookmarklet", cookies=ck).text
            assert "Bookmarklet<span" not in html          # a capture-box paste is not a bookmarklet capture
            r = client.post("/capture/web", data={"path": "page", "content": PAGE, "via": "bookmarklet"}, cookies=ck)
            assert r.status_code == 200
            html = client.get("/capture/setup?tab=bookmarklet", cookies=ck).text
            assert 'Bookmarklet<span class="cap-tab-st">just now' in html
            assert "when your last capture came in through that channel" in html
        finally:
            _cleanup("cp-via")
