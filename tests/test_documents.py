"""YONDER-179 / SPEC-8 D-196 — the documents store and file capture.

PDF/.ics/.eml only (by content), 25 MB per file, a per-user quota, ClamAV
required (stubbed) and fail-closed, the static PDF checks, owner-only listing
and download (as an attachment, never inline), the folder marker guard, trip
deletion removing its files, and each file type through processing."""
import io
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    import pypdf  # noqa: F401
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import authn, documents
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, Document, IngestBatch, TravelItem, Trip, TripMember,
                            TripTraveler, User)
    from extract import filescan
    from tests.test_filescan import _pdf

    Base.metadata.create_all(engine)
    client = TestClient(app)

    ICS = (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:doc-test-1\r\n"
           b"SUMMARY:Concert at the Hall\r\nDTSTART:20310510T200000\r\n"
           b"DTEND:20310510T230000\r\nLOCATION:Royal Hall\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

    def _eml(body="Your hotel is booked.", size=0):
        msg = EmailMessage()
        msg["Subject"] = "Booking confirmed"
        msg["From"] = "Hotel <noreply@example.com>"
        msg["Date"] = "Mon, 3 Aug 2026 12:00:00 -0400"
        msg.set_content(body + ("x" * size))
        return msg.as_bytes()

    @pytest.fixture(autouse=True)
    def store(tmp_path, monkeypatch):
        """A fresh store folder and a clean ClamAV for every test."""
        monkeypatch.setattr(settings, "documents_dir", str(tmp_path))
        monkeypatch.setattr(settings, "clamav_url", "http://av.test")
        monkeypatch.setattr(settings, "document_quota_mb", 1024)
        monkeypatch.setattr(settings, "document_max_mb", 25)
        monkeypatch.setattr(filescan, "clamav_scan", lambda url, name, data, timeout=30: None)
        yield tmp_path
        with SessionLocal() as s:
            s.query(Document).delete()
            for t in s.query(Trip).filter(Trip.name.like("Doc %")).all():
                s.query(TripMember).filter_by(trip_id=t.id).delete()
                s.query(TripTraveler).filter_by(trip_id=t.id).delete()
                s.delete(t)
            s.query(User).filter(User.username.like("doc-%")).delete(synchronize_session=False)
            s.commit()

    def _user(name, **kw):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                u = User(username=name, auth_source="local", **kw)
                s.add(u)
                s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _h(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _up(u, data, name="ticket.pdf", **form):
        return client.post("/api/v1/capture/file", headers=_h(u),
                           files={"file": (name, io.BytesIO(data))}, data=form)

    def _trip(owner, name="Doc trip", end="2031-05-12"):
        with SessionLocal() as s:
            t = Trip(name=name, source="manual", owner_id=owner.id,
                     start_date="2031-05-01", end_date=end)
            s.add(t)
            s.commit()
            return t.id

    # ---------------------------------------------------------------- storing
    def test_upload_stores_scans_and_captures(store):
        u = _user("doc-ana")
        r = _up(u, _pdf())
        assert r.status_code == 202, r.text
        body = r.json()
        doc = body["document"]
        assert doc["content_type"] == "application/pdf" and doc["filename"] == "ticket.pdf"
        assert body["duplicate"] is False and body["batch_id"]
        with SessionLocal() as s:
            d = s.get(Document, doc["id"])
            assert d.owner_id == u.id and d.batch_id == body["batch_id"]
            assert (store / u.id / d.sha256).read_bytes() == _pdf()
            b = s.get(IngestBatch, body["batch_id"])
            assert b.user_id == u.id and b.payload["capture_channel"] == "file"
            assert "file_base64" not in b.payload          # bytes stay in the store
        assert (store / documents.MARKER).exists()
        # The same file again: the first one back, no new capture.
        again = _up(u, _pdf(), name="copy.pdf").json()
        assert again["duplicate"] is True and again["document"]["id"] == doc["id"]
        with SessionLocal() as s:
            assert s.query(IngestBatch).filter(IngestBatch.user_id == u.id).count() == 1

    def test_types_come_from_content_not_names():
        u = _user("doc-types")
        assert _up(u, b"MZ\x90\x00 an exe", name="ticket.pdf").status_code == 415
        assert _up(u, ICS, name="whatever.bin").json()["document"]["content_type"] == "text/calendar"
        assert _up(u, _eml(), name="x.txt").json()["document"]["content_type"] == "message/rfc822"

    def test_size_and_quota_limits(monkeypatch):
        u = _user("doc-quota")
        monkeypatch.setattr(settings, "document_max_mb", 1)
        r = _up(u, _eml(size=1_100_000), name="big.eml")
        assert r.status_code == 413 and "at most 1 MB" in r.json()["detail"]
        monkeypatch.setattr(settings, "document_max_mb", 25)
        monkeypatch.setattr(settings, "document_quota_mb", 1)
        assert _up(u, _eml(size=600_000), name="a.eml").status_code == 202
        r = _up(u, _eml("Second booking.", size=600_000), name="b.eml")
        assert r.status_code == 413 and "storage is full" in r.json()["detail"]
        # An admin raises this person's quota (Administer → Users).
        admin = _user("doc-admin", is_admin=True)
        client.post(f"/users/{u.id}/quota", headers=_h(admin), data={"quota_mb": "5"})
        with SessionLocal() as s:
            assert s.get(User, u.id).document_quota_mb == 5
        assert _up(_user("doc-quota"), _eml("Second booking.", size=600_000),
                   name="b.eml").status_code == 202
        listing = client.get("/api/v1/documents", headers=_h(u)).json()
        assert listing["quota_bytes"] == 5 * 1024 * 1024 and listing["used_bytes"] > 1_200_000

    def test_scanning_is_required_and_fails_closed(monkeypatch, store):
        u = _user("doc-av")
        monkeypatch.setattr(filescan, "clamav_scan",
                            lambda url, name, data, timeout=30: "Eicar-Test-Signature")
        r = _up(u, _pdf())
        assert r.status_code == 422 and "Eicar-Test-Signature" in r.json()["detail"]

        def down(url, name, data, timeout=30):
            raise filescan.ScanUnavailable("URLError")
        monkeypatch.setattr(filescan, "clamav_scan", down)
        assert _up(u, _pdf()).status_code == 503
        monkeypatch.setattr(filescan, "clamav_scan", lambda url, name, data, timeout=30: None)
        bad = _pdf(catalog_extra=b"/OpenAction << /S /Launch /F (cmd.exe) >>")
        r = _up(u, bad)
        assert r.status_code == 422 and "Launch" in r.json()["detail"]
        assert not (store / u.id).exists() or not any((store / u.id).iterdir())
        # No scanner configured at all: file capture is off.
        monkeypatch.setattr(settings, "clamav_url", "")
        r = _up(u, _pdf())
        assert r.status_code == 503 and "TX_CLAMAV_URL" in r.json()["detail"]
        assert client.get("/api/v1/documents", headers=_h(u)).json()["enabled"] is False

    def test_a_missing_marker_with_files_on_record_stops_capture(store):
        """A drive that didn't mount: Docker hands over an empty folder."""
        u = _user("doc-mount")
        assert _up(u, _pdf()).status_code == 202
        (store / documents.MARKER).unlink()
        r = _up(u, ICS, name="c.ics")
        assert r.status_code == 503 and "isn't available" in r.json()["detail"]

    def test_off_without_a_folder(monkeypatch):
        monkeypatch.setattr(settings, "documents_dir", "")
        u = _user("doc-off")
        assert _up(u, _pdf()).status_code == 503
        st = client.get("/api/v1/documents", headers=_h(u)).json()
        assert st["enabled"] is False and "TX_DOCUMENTS_DIR" in st["reason"]

    # ---------------------------------------------------------------- who sees what
    def test_only_the_uploader_lists_downloads_or_deletes():
        owner, other = _user("doc-own"), _user("doc-other")
        admin = _user("doc-adm2", is_admin=True)
        tid = _trip(owner)
        with SessionLocal() as s:
            s.add(TripMember(trip_id=tid, user_id=other.id))       # shares the trip
            s.commit()
        doc = _up(owner, _pdf(), trip_id=tid).json()["document"]
        for who in (other, admin):
            assert client.get("/api/v1/documents", headers=_h(who)).json()["documents"] == []
            assert client.get(f"/api/v1/documents/{doc['id']}/download",
                              headers=_h(who)).status_code == 404
            assert client.delete(f"/api/v1/documents/{doc['id']}",
                                 headers=_h(who)).status_code == 404
        # A view-only sharer can't file a capture into the trip.
        assert _up(other, ICS, name="c.ics", trip_id=tid).status_code == 403
        r = client.get(f"/api/v1/documents/{doc['id']}/download", headers=_h(owner))
        assert r.status_code == 200 and r.content == _pdf()
        assert r.headers["content-disposition"].startswith("attachment;")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-security-policy"].startswith("sandbox")
        mine = client.get("/api/v1/documents", headers=_h(owner)).json()["documents"]
        assert [d["id"] for d in mine] == [doc["id"]]
        assert mine[0]["removed_on"] == "2032-05-12"            # trip end + 12 months

    def test_delete_and_purged_files(store):
        u = _user("doc-del")
        doc = _up(u, _pdf()).json()["document"]
        with SessionLocal() as s:
            d = s.get(Document, doc["id"])
            sha = d.sha256
            d.purged_at = datetime.now(timezone.utc)
            s.commit()
        assert client.get(f"/api/v1/documents/{doc['id']}/download",
                          headers=_h(u)).status_code == 410
        assert client.delete(f"/api/v1/documents/{doc['id']}", headers=_h(u)).status_code == 204
        assert not (store / u.id / sha).exists()
        with SessionLocal() as s:
            assert s.get(Document, doc["id"]) is None

    def test_deleting_a_trip_deletes_its_files(store):
        from app.trips.purge import purge_trip
        u = _user("doc-tripdel")
        tid = _trip(u, name="Doc gone")
        doc = _up(u, _pdf(), trip_id=tid).json()["document"]
        keep = _up(u, ICS, name="other.ics").json()["document"]
        with SessionLocal() as s:
            sha = s.get(Document, doc["id"]).sha256
            purge_trip(s, s.get(Trip, tid))
            s.commit()
            assert s.get(Document, doc["id"]) is None and s.get(Document, keep["id"])
        assert not (store / u.id / sha).exists()

    # ---------------------------------------------------------------- processing
    class _LLM:
        model = "fake"

        def __init__(self):
            self.docs = []

        def extract(self, doc, context):
            import json

            from tests.test_extract import VALID_BATCH
            self.docs.append((doc, context))
            return json.loads(json.dumps(VALID_BATCH)), []

    def _process(batch_id, llm=None):
        from app.processing import process_batch
        with SessionLocal() as s:
            b = s.get(IngestBatch, batch_id)
            status = process_batch(s, b, llm)
            items = s.query(TravelItem).filter_by(batch_id=batch_id).all()
            return status, items

    def test_each_file_type_is_extracted():
        u = _user("doc-proc")
        llm = _LLM()
        pdf_batch = _up(u, _pdf("Amtrak eTicket 7788"), notes="the train home").json()["batch_id"]
        status, items = _process(pdf_batch, llm)
        assert status == "extracted" and items and items[0].owner_id == u.id
        assert "Amtrak eTicket 7788" in llm.docs[0][0] and "the train home" in llm.docs[0][1]
        status, items = _process(_up(u, ICS, name="c.ics").json()["batch_id"])
        assert status == "extracted" and items[0].extraction["extractor"] == "ical"
        status, _ = _process(_up(u, _eml(), name="m.eml").json()["batch_id"], _LLM())
        assert status == "extracted"

    def test_a_removed_file_goes_to_review():
        u = _user("doc-gone")
        r = _up(u, _pdf()).json()
        with SessionLocal() as s:
            s.get(Document, r["document"]["id"]).purged_at = datetime.now(timezone.utc)
            s.commit()
        status, items = _process(r["batch_id"], _LLM())
        assert status == "needs_review"
        assert "no longer stored" in items[0].review["reasons"][0]

    # ---------------------------------------------------------------- the web page
    def test_web_upload_and_files_page(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        u = _user("doc-web")
        page = client.get("/capture", headers=_h(u)).text
        assert 'hx-post="/capture/file"' in page and "Your files" in page
        r = client.post("/capture/file", headers=_h(u),
                        files={"file": ("ticket.pdf", io.BytesIO(_pdf()))})
        assert r.status_code == 200 and "cap-msg err" not in r.text
        r = client.post("/capture/file", headers=_h(u),
                        files={"file": ("x.exe", io.BytesIO(b"MZ\x90\x00"))})
        assert "Only PDF" in r.text and "cap-msg err" in r.text
        files = client.get("/capture/files", headers=_h(u)).text
        assert "ticket.pdf" in files and "Removed on" in files
        with SessionLocal() as s:
            did = s.query(Document).filter_by(owner_id=u.id).one().id
        dl = client.get(f"/capture/files/{did}/download", headers=_h(u))
        assert dl.status_code == 200 and dl.headers["content-disposition"].startswith("attachment")
        stranger = _user("doc-web2")
        assert client.get(f"/capture/files/{did}/download",
                          headers=_h(stranger)).status_code == 404
        client.post(f"/capture/files/{did}/delete", headers=_h(stranger))
        with SessionLocal() as s:
            assert s.get(Document, did) is not None                # not theirs to delete
        client.post(f"/capture/files/{did}/delete", headers=_h(u))
        with SessionLocal() as s:
            assert s.get(Document, did) is None

    def test_removal_dates():
        from datetime import date
        from types import SimpleNamespace as T
        made = datetime(2026, 3, 1, tzinfo=timezone.utc)
        assert documents.keep_until(T(end_date="2026-10-20", start_date="2026-10-01"),
                                    made) == date(2027, 10, 20)
        assert documents.keep_until(T(end_date=None, start_date="2028-02-29"),
                                    made) == date(2029, 2, 28)
        assert documents.keep_until(None, made) == date(2027, 3, 1)
        assert documents.keep_until(T(end_date="", start_date=""), made) == date(2027, 3, 1)
