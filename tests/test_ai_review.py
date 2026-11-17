"""AI off in review, manual entry, and the AI call log (YONDER-354, SPEC-12 §6, D-228).

A capture no structured reader can read waits in review as "add the details by
hand", visible to the person who captured it; manual entry fills it through
the same schema gate and rules; every model call lands in ai_calls without its
prompt or reply; and the admin AI page shows it all, admins only.
"""
import base64
import io
import json
import sys
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
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
    from ai import client as ai_client
    from ai.client import Endpoint, chat_json
    from app import ai, authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import AiCall, Base, IngestBatch, TravelItem, User
    from app.processing import process_batch
    from extract.llm import LLMExtractor

    Base.metadata.create_all(engine)
    client = TestClient(app)

    @pytest.fixture(autouse=True)
    def _auth_on(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        yield

    def _user(admin=False):
        with SessionLocal() as s:
            u = User(username=f"air-{uuid.uuid4().hex[:8]}", auth_source="local",
                     is_admin=admin, email="traveller@example.test")
            s.add(u)
            s.commit()
            return u.id, {settings.auth_cookie_name: authn.issue_web_token(u)}

    def _email(body="Your stay at the Harbour Test Inn is confirmed for 4 nights."):
        msg = EmailMessage()
        msg["Subject"] = "Your booking at Harbour Test Inn"
        msg["From"] = "Harbour Test Inn <stay@example.test>"
        msg["Date"] = "Mon, 3 Aug 2026 12:00:00 -0400"
        msg.set_content(body)
        return base64.b64encode(msg.as_bytes()).decode()

    def _captured(user_id, llm=None) -> str:
        """A plain email captured by `user_id` and processed; returns the item id."""
        with SessionLocal() as s:
            b = IngestBatch(api_key_id="x", user_id=user_id, status="received",
                            payload={"capture_channel": "email",
                                     "raw_message_base64": _email()})
            s.add(b)
            s.commit()
            process_batch(s, b, llm)
            return s.query(TravelItem).filter_by(batch_id=b.id).one().id

    LODGING = {"item_type": "lodging", "f:/lodging/property_name": "Harbour Test Inn",
               "f:/lodging/check_in": "2031-05-10", "f:/lodging/check_out": "2031-05-14",
               "f:/lodging/place/city": "Testville", "f:/lodging/place/country": "CA",
               "f:/booking_status": "confirmed", "f:/booking_reference": "HTI-4421"}

    # ---------------------------------------------------------- AI off in review
    def test_a_capture_without_ai_waits_for_its_owner():
        uid, _ = _user()
        with SessionLocal() as s:
            item = s.get(TravelItem, _captured(uid))
            assert item.owner_id == uid                  # was None: admins only
            assert item.extraction is None
            assert item.review["reasons"][0] == "ai_off"
            assert "AI is off" in item.review["reasons"][1]

    def test_the_owner_sees_the_original_and_the_form():
        uid, cookie = _user()
        iid = _captured(uid)
        r = client.get(f"/review/item/{iid}", cookies=cookie)
        assert r.status_code == 200
        assert "Add the details by hand" in r.text
        assert "Your booking at Harbour Test Inn" in r.text       # subject
        assert "confirmed for 4 nights" in r.text                 # body
        r = client.get(f"/review/item/{iid}/manual?item_type=lodging", cookies=cookie)
        assert r.status_code == 200 and 'name="f:/lodging/property_name"' in r.text

    def test_manual_entry_refuses_what_is_missing_and_saves_nothing():
        uid, cookie = _user()
        iid = _captured(uid)
        r = client.post(f"/review/item/{iid}/manual", cookies=cookie,
                        data={"item_type": "lodging",
                              "f:/lodging/property_name": "Harbour Test Inn"})
        assert r.status_code == 200
        assert "Not saved yet" in r.text and "Check in is needed." in r.text
        assert 'value="Harbour Test Inn"' in r.text               # what was typed stays
        with SessionLocal() as s:
            assert s.get(TravelItem, iid).extraction is None

    def test_manual_entry_goes_through_the_gate_and_the_rules():
        uid, cookie = _user()
        iid = _captured(uid)
        r = client.post(f"/review/item/{iid}/manual", cookies=cookie, data=LODGING)
        assert r.status_code == 200 and "Not saved yet" not in r.text
        with SessionLocal() as s:
            item = s.get(TravelItem, iid)
            assert item.item_type == "lodging"
            assert item.extraction["extractor"] == "manual"
            p = item.extraction["payload"]
            assert p["lodging"]["property_name"] == "Harbour Test Inn"
            assert p["lodging"]["place"]["raw"] == "Testville, CA"
            assert p["lodging"]["check_in"]["origin"] == "document"   # copied from it
            assert "/lodging/property_name" in item.revision["field_locks"]
            assert item.status["extract"] == "ok"
            assert item.status["resolve"] != "pending"            # the re-resolve ran
            reasons = item.review.get("reasons") or []
            assert "ai_off" not in reasons and "missing_capture_context" not in reasons
        # Once it has details it's edited like any other item.
        r = client.post(f"/review/item/{iid}/manual", cookies=cookie, data=LODGING)
        assert r.status_code == 409 and "already has its details" in r.text

    def test_a_journey_without_an_arrival_time_has_no_date_yet():
        from app.review import manual
        payload, _, problems = manual.build("ground_transport", {
            "/ground_transport/mode": "train",
            "/ground_transport/departure/time": "2031-05-10 09:15",
            "/ground_transport/departure/place/city": "Testville",
            "/ground_transport/arrival/place/city": "Otherton"})
        assert problems == []
        assert payload["ground_transport"]["arrival"]["time"] == {"precision": "none"}

    def test_another_user_cant_see_or_fill_it():
        uid, _ = _user()
        _, stranger = _user()
        iid = _captured(uid)
        assert client.get(f"/review/item/{iid}", cookies=stranger).status_code == 404
        assert client.get(f"/review/item/{iid}/manual?item_type=lodging",
                          cookies=stranger).status_code == 404
        assert client.post(f"/review/item/{iid}/manual", cookies=stranger,
                           data=LODGING).status_code == 404
        with SessionLocal() as s:
            assert s.get(TravelItem, iid).extraction is None

    # ---------------------------------------------------------- the call log
    class _Fake:
        def __init__(self, reply):
            self.reply = reply

        def __call__(self, req, timeout=None):
            body = io.BytesIO(json.dumps(self.reply).encode())
            body.status = 200
            body.__enter__ = lambda *a: body
            body.__exit__ = lambda *a: False
            return body

    def _reply(content):
        return {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30}}

    _LLM = Endpoint(provider="openai", url="http://litellm.test:4000/v1", model="m-log",
                    api_key="sk-test-not-real", feature="discover", user_id="u-log")

    def test_each_call_is_logged_without_its_content(monkeypatch):
        monkeypatch.setattr(ai_client.urllib.request, "urlopen", _Fake(_reply('{"secret": 1}')))
        chat_json(_LLM, [{"role": "user", "content": "Harbour Test Inn"}], {}, name="food")
        with SessionLocal() as s:
            row = s.query(AiCall).filter_by(user_id="u-log").order_by(
                AiCall.started_at.desc()).first()
            assert (row.feature, row.function, row.provider, row.model, row.ok) == \
                ("discover", "food", "openai", "m-log", True)
            assert (row.prompt_tokens, row.out_tokens) == (120, 30)
            stored = json.dumps({c.name: str(getattr(row, c.name))
                                 for c in AiCall.__table__.columns})
        assert "Harbour" not in stored and "secret" not in stored and "sk-test" not in stored

    def test_a_failed_call_logs_its_class_only(monkeypatch):
        def refuse(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized: sk-test-not-real",
                                         {}, io.BytesIO(b""))
        monkeypatch.setattr(ai_client.urllib.request, "urlopen", refuse)
        llm = Endpoint(provider="openai", url="http://litellm.test:4000/v1", model="m-fail",
                       feature="extraction", user_id="u-fail")
        with pytest.raises(urllib.error.HTTPError):
            chat_json(llm, [], {})
        with SessionLocal() as s:
            row = s.query(AiCall).filter_by(user_id="u-fail").one()
            assert row.ok is False and row.error == "HTTPError"

    def test_a_broken_log_never_breaks_the_call(monkeypatch):
        monkeypatch.setattr(ai_client.urllib.request, "urlopen", _Fake(_reply("{}")))
        monkeypatch.setattr(ai_client, "_observers", [lambda rec: 1 / 0])
        assert chat_json(_LLM, [], {}).content == "{}"

    def test_extraction_calls_are_logged_against_the_capture_owner(monkeypatch):
        monkeypatch.setattr(ai_client.urllib.request, "urlopen", _Fake(_reply("not json")))
        uid, _ = _user()
        x = LLMExtractor(Endpoint(provider="openai", url="http://litellm.test:4000/v1",
                                  model="m-x", feature="extraction"))
        assert x.model == "m-x" and x.for_user(uid).llm.user_id == uid and x.llm.user_id is None
        _captured(uid, llm=x)                         # two schema-invalid replies
        with SessionLocal() as s:
            rows = s.query(AiCall).filter_by(user_id=uid).all()
            assert len(rows) == 2 and {r.function for r in rows} == {"travel_extraction"}

    def test_an_extracted_item_records_its_model(monkeypatch):
        from tests.test_extract import VALID_BATCH
        monkeypatch.setattr(ai_client.urllib.request, "urlopen",
                            _Fake(_reply(json.dumps(VALID_BATCH))))
        uid, _ = _user()
        x = LLMExtractor(Endpoint(provider="openai", url="http://litellm.test:4000/v1",
                                  model="m-ok", feature="extraction"))
        with SessionLocal() as s:
            item = s.get(TravelItem, _captured(uid, llm=x))
            assert item.extraction["extractor"] == "llm" and item.extraction["model"] == "m-ok"

    def test_calls_older_than_90_days_are_purged():
        old = datetime.now(timezone.utc) - timedelta(days=91)
        with SessionLocal() as s:
            s.add(AiCall(feature="discover", function="food", provider="openai", model="m",
                         started_at=old, duration_ms=1, ok=True, user_id="u-old"))
            s.add(AiCall(feature="discover", function="food", provider="openai", model="m",
                         duration_ms=1, ok=True, user_id="u-new"))
            s.commit()
            assert ai.purge_calls(s) >= 1
            assert s.query(AiCall).filter_by(user_id="u-old").count() == 0
            assert s.query(AiCall).filter_by(user_id="u-new").count() == 1

    # ---------------------------------------------------------- the admin page
    def test_the_ai_page_is_for_admins_only():
        _, cookie = _user(admin=False)
        assert client.get("/settings/ai", cookies=cookie).status_code == 403
        assert client.post("/settings/ai/test", cookies=cookie).status_code == 403

    def test_the_ai_page_shows_off_and_whats_waiting(monkeypatch):
        monkeypatch.setattr(settings, "ai_enabled", False)
        uid, _ = _user()
        _captured(uid)
        _, admin = _user(admin=True)
        r = client.get("/settings/ai", cookies=admin)
        assert r.status_code == 200
        assert "<b>Off.</b>" in r.text and "Waiting for details by hand" in r.text
        assert "sk-" not in r.text

    def test_test_connection_says_what_happened(monkeypatch):
        monkeypatch.setattr(settings, "ai_enabled", True)
        monkeypatch.setattr(settings, "ai_provider", "openai")
        monkeypatch.setattr(settings, "ai_url", "http://litellm.test:4000/v1")
        monkeypatch.setattr(settings, "ai_model_chat", "m-test")
        monkeypatch.setattr(settings, "ai_api_key", "sk-test-not-real")
        responses = iter([{"data": []}, _reply('{"ok": true}')])
        monkeypatch.setattr(ai_client.urllib.request, "urlopen",
                            lambda req, timeout=None: _Fake(next(responses))(req))
        _, admin = _user(admin=True)
        r = client.post("/settings/ai/test", cookies=admin)
        assert r.status_code == 200 and "m-test answered." in r.text
        assert "sk-test-not-real" not in r.text
        with SessionLocal() as s:
            assert s.query(AiCall).filter_by(feature="admin_test", model="m-test").count() >= 1
