"""D-200 (YONDER-228): the sidebar is Home · Trips · Journal | Capture · Places · Inbox,
a global Capture button opens a sheet from any page, the review queue is the Inbox at
/inbox (with /review redirecting), and a trip says how many of its captures wait there."""
import re
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
    from app.models import Base, TravelItem, Trip, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    def _user(name):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                u = User(username=name, auth_source="local")
                s.add(u)
                s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _bearer(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _capture(owner_id, trip_id, headline):
        """A capture filed to a trip and waiting for a trip review (the 'waiting' bucket
        needs no resolution): extracted, resolution pending."""
        with SessionLocal() as s:
            i = TravelItem(owner_id=owner_id, trip_id=trip_id, item_type="lodging",
                           source={}, status={"extract": "ok", "resolve": "ok"},
                           extraction={"payload": {"item_type": "lodging",
                                                   "lodging": {"property_name": headline}}},
                           resolution={"outcome": "deferred"})
            s.add(i)
            s.commit()
            return i.id

    def test_sidebar_order_divider_and_capture_button():
        html = client.get("/").text
        nav = html[html.index('aria-label="Sections"'):]
        nav = nav[:nav.index("</nav>")]
        labels = re.findall(r'</span>(Home|Trips|Journal|Capture Hub|Capture|Places|Inbox)<', nav)
        assert labels == ["Home", "Trips", "Journal", "Capture Hub", "Places", "Inbox"]   # D-221
        assert nav.index('class="navsep"') < nav.index(">Capture")      # the divider
        assert 'class="navcap"' in html and 'hx-get="/capture/sheet"' in html
        assert ">Review<" not in nav

    def test_review_redirects_to_inbox_keeping_the_query():
        r = client.get("/review?bucket=waiting", follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == "/inbox?bucket=waiting"
        page = client.get("/inbox")
        assert page.status_code == 200 and '<h1 class="pagetitle">Inbox</h1>' in page.text
        assert "<title>Inbox" in page.text

    def test_capture_sheet_posts_to_the_capture_pipeline():
        html = client.get("/capture/sheet").text
        assert 'role="dialog"' in html and 'hx-post="/capture/web"' in html
        assert 'name="content"' in html and 'href="/capture"' in html

    def test_trip_says_what_waits_in_the_inbox_and_the_inbox_narrows_to_it():
        owner, stranger = _user("nav-owner"), _user("nav-stranger")
        with SessionLocal() as s:
            t = Trip(name="Nav trip", source="manual", owner_id=owner.id)
            other = Trip(name="Nav other", source="manual", owner_id=owner.id)
            s.add_all([t, other])
            s.commit()
            tid, oid = t.id, other.id
        mine = _capture(owner.id, tid, "Nav Riad")
        elsewhere = _capture(owner.id, oid, "Nav Elsewhere")
        settings.auth_required = True
        try:
            page = client.get(f"/trips/{tid}", headers=_bearer(owner)).text
            assert f'href="/inbox?bucket=all&amp;trip={tid}">1 waiting in Inbox</a>' in page
            inbox = client.get(f"/inbox?bucket=all&trip={tid}", headers=_bearer(owner)).text
            assert "Nav Riad" in inbox and "Nav Elsewhere" not in inbox
            assert "Nav trip" in inbox and "Show everything" in inbox
            # Someone who can't see the trip gets the unfiltered (own) Inbox, no name leaked.
            other_view = client.get(f"/inbox?bucket=all&trip={tid}", headers=_bearer(stranger)).text
            assert "Nav trip" not in other_view and "Nav Riad" not in other_view
        finally:
            settings.auth_required = False
            with SessionLocal() as s:
                s.query(TravelItem).filter(TravelItem.id.in_([mine, elsewhere])).delete(
                    synchronize_session=False)
                s.query(Trip).filter(Trip.id.in_([tid, oid])).delete(synchronize_session=False)
                s.commit()
