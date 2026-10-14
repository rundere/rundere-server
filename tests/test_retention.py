"""YONDER-179 part 2 / SPEC-8 D-196 — the daily retention sweep.

Stored files and raw captures go 12 months after their trip ends (or after
they arrived, with no trip); what was extracted stays; captures still in
review are never purged; a purged capture can't be re-processed; the mailbox
copies go to Trash (Captured with the capture, Dropped after 30 days), only
from Rundere's own two folders, never expunged; a failed mailbox step is
retried the next day."""
import base64
import sys
import uuid
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import documents, inbox, retention
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, Document, IngestBatch, TravelItem, Trip, User

    Base.metadata.create_all(engine)
    client = TestClient(app)
    TODAY = date(2027, 6, 1)

    @pytest.fixture(autouse=True)
    def store(tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "documents_dir", str(tmp_path))
        monkeypatch.setattr(settings, "retention_months", 12)
        monkeypatch.setattr(settings, "inbox_dropped_days", 30)
        monkeypatch.setattr(settings, "inbox_trash_folder", "")
        monkeypatch.setattr(settings, "inbox_done_folder", "Folders/Rundere Captured")
        monkeypatch.setattr(settings, "inbox_dropped_folder", "Folders/Rundere Dropped")
        monkeypatch.setattr(inbox, "configured", lambda: True)
        yield tmp_path
        with SessionLocal() as s:
            ids = [b.id for b in s.query(IngestBatch).all()
                   if (b.payload or {}).get("capture_context", {}).get("rt")]
            s.query(TravelItem).filter(TravelItem.batch_id.in_(ids)).delete(
                synchronize_session=False)
            s.query(IngestBatch).filter(IngestBatch.id.in_(ids)).delete(
                synchronize_session=False)
            s.query(Document).delete()
            s.query(Trip).filter(Trip.name.like("Ret %")).delete(synchronize_session=False)
            s.query(User).filter(User.username.like("ret-%")).delete(synchronize_session=False)
            s.commit()

    def _user(name="ret-ana"):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                u = User(username=name, auth_source="local")
                s.add(u)
                s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _trip(end):
        with SessionLocal() as s:
            t = Trip(name=f"Ret {uuid.uuid4().hex[:6]}", source="manual",
                     start_date=end, end_date=end)
            s.add(t)
            s.commit()
            return t.id

    def _raw(mid):
        m = EmailMessage()
        m["Subject"], m["From"], m["Message-ID"] = "Hotel booked", "Hotel <h@example.com>", mid
        m["Date"] = "Mon, 3 Aug 2026 12:00:00 -0400"
        m.set_content("Your stay is confirmed. Card ending 4242.")
        return base64.b64encode(m.as_bytes()).decode()

    def _capture(trip_id=None, *, state="logged", channel="email", received=None):
        """An email (or web) capture with one item in the given review state."""
        mid = f"<{uuid.uuid4().hex}@example.com>"
        payload = {"capture_channel": channel, "message_id": mid,
                   "capture_context": {"rt": True}}
        if channel == "web":
            payload["html"] = "<html>booking page</html>"
        else:
            payload["raw_message_base64"] = _raw(mid)
        review, commit, resolution = {}, None, {}
        if state == "logged":
            resolution = {"targets": ["rundere.activity"]}
            commit = {"dry_run": False, "results": [{"target": "rundere.activity", "ok": True}]}
        elif state == "ignored":
            review = {"action_taken": "ignored"}
        with SessionLocal() as s:
            b = IngestBatch(api_key_id=_key(s), payload=payload, status="extracted",
                            received_at=received or datetime(2026, 1, 5, tzinfo=timezone.utc))
            s.add(b)
            s.flush()
            s.add(TravelItem(batch_id=b.id, item_type="lodging", trip_id=trip_id,
                             schema_version="1.4.0", review=review, commit=commit,
                             resolution=resolution, status={"extract": "ok"}))
            s.commit()
            return b.id, mid

    def _key(s):
        from app.capture import _internal_key_id
        return _internal_key_id(s)

    def _payload(bid):
        with SessionLocal() as s:
            return s.get(IngestBatch, bid).payload

    class FakeIMAP:
        """Rundere's two folders plus someone's personal INBOX and Trash."""
        capabilities = ("MOVE", "UIDPLUS", "IDLE")

        def __init__(self, folders):
            self.folders = {k: list(v) for k, v in folders.items()}
            self.cur = None
            self.selected = []

        def list(self):
            return "OK", [b'(\\HasNoChildren \\Trash) "/" "Trash"',
                          b'(\\HasNoChildren) "/" "INBOX"',
                          b'(\\HasNoChildren) "/" "Folders/Rundere Captured"']

        def select(self, q):
            self.cur = q.strip('"')
            self.selected.append(self.cur)
            return "OK", [str(len(self.folders.get(self.cur, []))).encode()]

        def create(self, q):
            self.folders.setdefault(q.strip('"'), [])
            return "OK", []

        def uid(self, cmd, *args):
            msgs = self.folders.get(self.cur, [])
            if cmd == "SEARCH":
                if args[1] == "HEADER":
                    mid = args[3].strip('"')
                    hits = [m["uid"] for m in msgs if m["mid"] == mid]
                else:                                       # BEFORE dd-Mon-YYYY
                    cutoff = datetime.strptime(args[2], "%d-%b-%Y").date()
                    hits = [m["uid"] for m in msgs if m["arrived"] < cutoff]
                return "OK", [b" ".join(hits)]
            if cmd == "MOVE":
                uid, dest = args[0], args[1].strip('"')
                m = next(m for m in msgs if m["uid"] == uid)
                msgs.remove(m)
                self.folders.setdefault(dest, []).append(m)
                return "OK", []
            raise AssertionError(f"unexpected {cmd}")      # never EXPUNGE, never STORE

        def logout(self):
            pass

    def _msg(uid, mid="<x@y>", arrived=date(2027, 1, 1)):
        return {"uid": uid, "mid": mid, "arrived": arrived}

    # ---------------------------------------------------------------- files
    def test_files_go_twelve_months_after_the_trip(store):
        u = _user()
        old_trip, new_trip = _trip("2026-05-01"), _trip("2027-01-01")
        made = datetime(2026, 4, 1, tzinfo=timezone.utc)
        with SessionLocal() as s:
            docs = []
            for name, trip in (("old", old_trip), ("new", new_trip), ("loose", None)):
                d = Document(owner_id=u.id, sha256=name * 8, content_type=documents.PDF,
                             size_bytes=3, filename=f"{name}.pdf", trip_id=trip,
                             created_at=made)
                s.add(d)
                (store / u.id).mkdir(exist_ok=True)
                (store / u.id / d.sha256).write_bytes(b"pdf")
                docs.append(d)
            s.commit()
            ids = {d.filename: d.id for d in docs}
        dry = retention.run(SessionLocal, today=TODAY, dry_run=True, mailbox=False)
        assert dry.files_purged == 2 and (store / u.id / ("old" * 8)).exists()
        report = retention.run(SessionLocal, today=TODAY, mailbox=False)
        assert report.files_purged == 2                 # old (trip+12) and loose (upload+12)
        with SessionLocal() as s:
            assert s.get(Document, ids["old.pdf"]).purged_at is not None
            assert s.get(Document, ids["loose.pdf"]).purged_at is not None
            assert s.get(Document, ids["new.pdf"]).purged_at is None
        assert not (store / u.id / ("old" * 8)).exists()
        assert (store / u.id / ("new" * 8)).exists()
        assert retention.run(SessionLocal, today=TODAY, mailbox=False).files_purged == 0

    # ---------------------------------------------------------------- raw captures
    def test_raw_captures_are_tombstoned_but_never_while_in_review():
        ended = _trip("2026-03-01")
        logged, mid = _capture(ended)
        ignored, _ = _capture(ended, state="ignored")
        pending, _ = _capture(ended, state="pending")
        web, _ = _capture(ended, channel="web")
        recent, _ = _capture(_trip("2027-03-01"))
        report = retention.run(SessionLocal, today=TODAY, mailbox=False)
        assert report.captures_purged == 3 and report.captures_held == 1
        t = _payload(logged)
        assert "raw_message_base64" not in t and t["purged_at"]
        assert t["subject"] == "Hotel booked" and t["sender"] == "Hotel <h@example.com>"
        assert t["message_id"] == mid and t["mailbox_pending"] is True
        assert "4242" not in str(t)                      # no content survives
        assert "html" not in _payload(web)
        assert "raw_message_base64" in _payload(pending)    # still in review: kept
        assert "raw_message_base64" in _payload(recent)     # not due yet
        assert _payload(ignored)["purged_at"]
        with SessionLocal() as s:
            assert s.query(TravelItem).filter_by(batch_id=logged).count() == 1  # items stay
            assert retention.source_gone(s, _payload(logged))
            assert not retention.source_gone(s, _payload(recent))

    def test_no_trip_counts_from_arrival():
        early, _ = _capture(None, received=datetime(2026, 5, 1, tzinfo=timezone.utc))
        late, _ = _capture(None, received=datetime(2026, 7, 1, tzinfo=timezone.utc))
        retention.run(SessionLocal, today=TODAY, mailbox=False)
        assert _payload(early).get("purged_at") and not _payload(late).get("purged_at")

    def test_a_removed_original_shows_on_the_review_screen_and_blocks_reprocess(monkeypatch):
        bid, _ = _capture(_trip("2026-03-01"), state="ignored")
        retention.run(SessionLocal, today=TODAY, mailbox=False)
        with SessionLocal() as s:
            from app.review.routes import _original_context
            item = s.query(TravelItem).filter_by(batch_id=bid).one()
            assert _original_context(s, item) == {"removed": True}
        kept, _ = _capture(_trip("2027-04-01"), state="pending")
        with SessionLocal() as s:
            from app.review.routes import _original_context
            item = s.query(TravelItem).filter_by(batch_id=kept).one()
            assert _original_context(s, item) == {"removed": False, "until": "2028-04-01"}

    # ---------------------------------------------------------------- mailbox
    def test_mailbox_copies_go_to_trash_from_rundere_folders_only():
        _, mid = _capture(_trip("2026-03-01"))
        _, keep_mid = _capture(_trip("2027-04-01"))
        imap = FakeIMAP({
            "Folders/Rundere Captured": [_msg(b"1", mid), _msg(b"2", keep_mid)],
            "Folders/Rundere Dropped": [_msg(b"7", arrived=date(2027, 4, 1)),
                                        _msg(b"8", arrived=date(2027, 5, 20))],
            "INBOX": [_msg(b"1", mid, arrived=date(2020, 1, 1))],   # personal mail
        })
        report = retention.run(SessionLocal, today=TODAY, connect=lambda: imap)
        assert report.mail_trashed == 1 and report.dropped_trashed == 1
        assert [m["uid"] for m in imap.folders["Trash"]] == [b"1", b"7"]
        assert [m["uid"] for m in imap.folders["Folders/Rundere Captured"]] == [b"2"]
        assert [m["uid"] for m in imap.folders["Folders/Rundere Dropped"]] == [b"8"]
        assert len(imap.folders["INBOX"]) == 1                     # never touched
        assert "INBOX" not in imap.selected
        # Done: tomorrow's run doesn't look for it again.
        with SessionLocal() as s:
            assert not any((b.payload or {}).get("mailbox_pending")
                           for b in s.query(IngestBatch).all())

    def test_a_failed_mailbox_step_is_retried_next_day():
        bid, mid = _capture(_trip("2026-03-01"))

        def down():
            raise OSError("bridge down")
        report = retention.run(SessionLocal, today=TODAY, connect=down)
        assert report.captures_purged == 1 and "failed" in report.mail_skipped
        assert _payload(bid)["mailbox_pending"] is True
        imap = FakeIMAP({"Folders/Rundere Captured": [_msg(b"3", mid)]})
        report = retention.run(SessionLocal, today=TODAY, connect=lambda: imap)
        assert report.captures_purged == 0 and report.mail_trashed == 1
        assert "mailbox_pending" not in _payload(bid)

    def test_no_trash_folder_skips_the_mailbox(monkeypatch):
        _capture(_trip("2026-03-01"))
        imap = FakeIMAP({"Folders/Rundere Captured": []})
        imap.list = lambda: ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])
        report = retention.run(SessionLocal, today=TODAY, connect=lambda: imap)
        assert "Trash" in report.mail_skipped and "Trash" not in imap.folders
        monkeypatch.setattr(settings, "inbox_trash_folder", "Deleted Items")
        report = retention.run(SessionLocal, today=TODAY, connect=lambda: imap)
        assert report.mail_skipped is None

    def test_no_inbox_no_mailbox_step(monkeypatch):
        monkeypatch.setattr(inbox, "configured", lambda: False)
        report = retention.run(SessionLocal, today=TODAY,
                               connect=lambda: pytest.fail("must not connect"))
        assert "isn't configured" in report.mail_skipped
