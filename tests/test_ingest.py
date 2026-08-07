"""HANDOFF Task 5: 'Write tests for the auth failure paths, including replay.'

Runs against SQLite via TX_DATABASE_URL override — no Postgres needed for the
auth logic. Requires fastapi/httpx; on a host without them the module skips
(pytest) / prints a notice (standalone). CI and the container run the full set.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="tx-test-")
os.environ.setdefault("TX_DATABASE_URL", f"sqlite:///{_TMP}/test.sqlite3")
os.environ.setdefault("TX_INGEST_RATE_LIMIT", "5")

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import auth
    from app.db import SessionLocal, engine
    from app.main import app, load_enablement, seed_rule_settings
    from app.models import ApiKey, Base, IngestBatch

    Base.metadata.create_all(engine)
    client = TestClient(app)

    KEY = auth.generate_key()
    with SessionLocal() as s:
        s.add(ApiKey(client_name="test-apps-script", key_hash=auth.hash_key(KEY)))
        s.commit()

    def _post(body: dict | bytes, key=None, ts=None, sig=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        key = KEY if key is None else key
        ts = str(int(time.time())) if ts is None else ts
        sig = auth.sign(key, ts, raw) if sig is None else sig
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-Api-Key"] = key
        headers["X-Timestamp"] = ts
        headers["X-Signature"] = sig
        return client.post("/ingest", content=raw, headers=headers)


def _skip_or(fn):
    if not DEPS:
        import pytest
        pytest.skip("fastapi/httpx not installed on this host")
    return fn()


def test_health():
    def run():
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["rules_registered"] == 57
    _skip_or(run)


def test_missing_key_rejected():
    def run():
        assert _post({"a": 1}, key="").status_code == 401
    _skip_or(run)


def test_unknown_key_rejected():
    def run():
        r = _post({"a": 1}, key="tx_" + "0" * 40)
        assert r.status_code == 401
        assert "unknown" in r.json()["error"]
    _skip_or(run)


def test_revoked_key_rejected():
    def run():
        from datetime import datetime, timezone
        rk = auth.generate_key()
        with SessionLocal() as s:
            s.add(ApiKey(client_name="revoked-client", key_hash=auth.hash_key(rk),
                         revoked_at=datetime.now(timezone.utc)))
            s.commit()
        assert _post({"a": 1}, key=rk).status_code == 401
    _skip_or(run)


def test_stale_timestamp_rejected_replay_guard():
    def run():
        old = str(int(time.time()) - 600)          # 10 min old > 5 min window
        r = _post({"a": 1}, ts=old)
        assert r.status_code == 401
        assert "replay" in r.json()["error"]
    _skip_or(run)


def test_future_timestamp_rejected():
    def run():
        assert _post({"a": 1}, ts=str(int(time.time()) + 3600)).status_code == 401
    _skip_or(run)


def test_bad_signature_rejected():
    def run():
        r = _post({"a": 1}, sig="deadbeef" * 8)
        assert r.status_code == 401
        assert "signature" in r.json()["error"]
    _skip_or(run)


def test_tampered_body_rejected():
    """Signature computed over one body, a different body sent — the actual
    interception scenario the HMAC exists for."""
    def run():
        ts = str(int(time.time()))
        sig = auth.sign(KEY, ts, json.dumps({"a": 1}).encode())
        r = _post({"a": 2}, ts=ts, sig=sig)
        assert r.status_code == 401
    _skip_or(run)


def test_valid_request_gets_202_and_is_stored_not_processed():
    def run():
        r = _post({"declared_segment_count": 2, "items": []})
        assert r.status_code == 202
        bid = r.json()["batch_id"]
        with SessionLocal() as s:
            row = s.get(IngestBatch, bid)
            assert row is not None and row.status == "received"
            assert row.declared_segment_count == 2
            assert row.artifact_sha256               # content-addressed (D-15)
    _skip_or(run)


def test_non_json_body_rejected_after_auth():
    def run():
        assert _post(b"not json at all").status_code == 400
    _skip_or(run)


def test_rate_limit_returns_429():
    def run():
        from app.config import settings
        n = settings.ingest_rate_limit + 3   # read the effective limit, not a
        codes = [_post({"n": i}).status_code for i in range(n)]  # literal —
        assert 429 in codes                  # config/import order can shift it
        assert codes[0] in (202, 429) and codes[-1] == 429
    _skip_or(run)


def test_rule_settings_seeded_and_loadable():
    """D-25 wiring: startup seeds 57 rows; load_enablement feeds rules.py."""
    def run():
        with SessionLocal() as s:
            seed_rule_settings(s)
            en = load_enablement(s)
        assert len(en) == 57
        assert set(en.values()) == {"enforce"}
    _skip_or(run)


if __name__ == "__main__":
    if not DEPS:
        print("SKIP: fastapi/httpx not installed on this host — run in container/CI")
        sys.exit(0)
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as ex:
                failures += 1
                print(f"  FAIL {name}: {str(ex)[:300]}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
