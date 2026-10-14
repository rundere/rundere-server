"""SPEC-9 D-158: a capture on the capture page starts processing by itself. Approval
stays human (D-31); capture never depends on processing."""
import json
import sys
import threading
import time
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
    from app.models import Base, IngestBatch, TravelItem, User
    from app.review import autoprocess, jobs

    Base.metadata.create_all(engine)
    client = TestClient(app)

    _LD = json.dumps({
        "@context": "https://schema.org", "@type": "LodgingReservation",
        "reservationNumber": "AP123", "checkinDate": "2027-03-20", "checkoutDate": "2027-03-23",
        "reservationFor": {"@type": "LodgingBusiness", "name": "Autoprocess Riad",
                           "address": {"@type": "PostalAddress", "addressLocality": "Harar",
                                       "addressCountry": "MA"}}})
    PAGE = ('<!doctype html><html><head><script type="application/ld+json">'
            f'{_LD}</script></head><body><h1>Your stay</h1></body></html>')

    def _wait_finished(job_id, secs=10):
        end = time.time() + secs
        while time.time() < end:
            j = jobs.get(job_id)
            if j and j["finished"]:
                return j
            time.sleep(0.02)
        raise AssertionError("job never finished")

    def _reset():
        with autoprocess._LOCK:
            autoprocess._ACTIVE = False
            autoprocess._AGAIN.clear()

    def test_disabled_starts_nothing(monkeypatch):
        _reset()
        monkeypatch.setattr(settings, "auto_process", False)
        calls = []
        monkeypatch.setattr(autoprocess, "_run_once", lambda *a: calls.append(a))
        assert autoprocess.kick(None) is None and not calls

    def test_kick_runs_for_that_user_and_clears_the_flag(monkeypatch):
        _reset()
        monkeypatch.setattr(settings, "auto_process", True)
        seen = []
        monkeypatch.setattr(autoprocess, "_run_once",
                            lambda snap, progress: (seen.append(snap), ("Done", None))[1])
        u = type("U", (), {"id": "u1", "is_admin": False})()
        jid = autoprocess.kick(u)
        assert jid and _wait_finished(jid)["toast"] == "Done"
        assert [s.id for s in seen] == ["u1"] and autoprocess._ACTIVE is False

    def test_capture_during_a_run_is_processed_before_it_ends(monkeypatch):
        _reset()
        monkeypatch.setattr(settings, "auto_process", True)
        gate, order = threading.Event(), []

        def fake(snap, progress):
            order.append(snap.id)
            if len(order) == 1:
                gate.wait(5)
            return "Done", None
        monkeypatch.setattr(autoprocess, "_run_once", fake)
        a = type("U", (), {"id": "ua", "is_admin": False})()
        b = type("U", (), {"id": "ub", "is_admin": False})()
        jid = autoprocess.kick(a)
        time.sleep(0.1)
        assert autoprocess.kick(b) is None             # queued behind our running job
        assert "ub" in autoprocess._AGAIN
        gate.set()
        _wait_finished(jid)
        assert order == ["ua", "ub"] and autoprocess._ACTIVE is False and not autoprocess._AGAIN

    def test_a_failing_run_does_not_wedge_later_captures(monkeypatch):
        _reset()
        monkeypatch.setattr(settings, "auto_process", True)
        monkeypatch.setattr(autoprocess, "_run_once",
                            lambda *a: (_ for _ in ()).throw(RuntimeError("llm down")))
        jid = autoprocess.kick(None)
        assert "Failed" in _wait_finished(jid)["toast"] and autoprocess._ACTIVE is False
        monkeypatch.setattr(autoprocess, "_run_once", lambda *a: ("Done", None))
        assert autoprocess.kick(None)                  # a new job can start

    def _user(name):
        with SessionLocal() as s:
            u = User(username=name, auth_source="local")
            s.add(u); s.commit()
            return u.id, authn.issue_access_token(u)

    def _cleanup(*names):
        with SessionLocal() as s:
            uids = [u.id for u in s.query(User).filter(User.username.in_(names)).all()]
            bids = [b.id for b in s.query(IngestBatch).filter(IngestBatch.user_id.in_(uids)).all()]
            s.query(TravelItem).filter(TravelItem.batch_id.in_(bids)).delete(synchronize_session=False)
            s.query(IngestBatch).filter(IngestBatch.id.in_(bids)).delete(synchronize_session=False)
            s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
            s.commit()

    def test_capture_page_flow_lands_in_the_queue_unapproved_and_is_private(monkeypatch):
        _reset()
        monkeypatch.setattr(settings, "auto_process", True)
        monkeypatch.setattr(settings, "auth_required", True)
        _, tok_a = _user("ap-alice")
        _, tok_b = _user("ap-bob")
        ck = lambda t: {settings.auth_cookie_name: t}              # noqa: E731
        try:
            r = client.post("/capture/web", data={"content": PAGE, "path": "page"}, cookies=ck(tok_a))
            assert r.status_code == 200 and "/capture/batch/" in r.text and "Reading it now" in r.text
            bid = r.text.split("/capture/batch/")[1].split("?")[0]
            html = ""
            for _ in range(200):
                html = client.get(f"/capture/batch/{bid}", cookies=ck(tok_a)).text
                if "cap-cards" in html or "nothing could be read" in html:
                    break
                time.sleep(0.05)
            assert "Autoprocess Riad" in html and "waiting for your approval" in html
            assert "Open in Inbox" in html                 # D-200
            with SessionLocal() as s:
                items = s.query(TravelItem).filter(TravelItem.batch_id == bid).all()
                assert items and all((i.review or {}).get("action_taken") != "approved"
                                     for i in items)                # D-31: never approved here
            # another person can't read this capture's result
            assert client.get(f"/capture/batch/{bid}", cookies=ck(tok_b)).status_code == 404
            # nor can an anonymous caller when sign-in is required
            assert client.get(f"/capture/batch/{bid}").status_code == 404
        finally:
            _cleanup("ap-alice", "ap-bob")

    def test_with_auto_process_off_the_page_says_how_to_process(monkeypatch):
        _reset()
        monkeypatch.setattr(settings, "auto_process", False)
        r = client.post("/capture/web", data={"content": PAGE, "path": "page"})
        assert "Process new captures" in r.text and "hx-get" not in r.text
        with SessionLocal() as s:
            s.query(IngestBatch).filter(IngestBatch.user_id.is_(None),
                                        IngestBatch.status == "received").delete(synchronize_session=False)
            s.commit()
