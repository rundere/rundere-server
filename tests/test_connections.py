"""YONDER-192 — per-user AirTrail / AdventureLog connections (SPEC-8 D-150–D-152).

- Secrets at rest (D-151): AES-256-GCM, bound to the row, a rotatable keyring,
  write-only, never in a page, a log or commit_attempts.
- Outbound URL safety (D-152): https and public addresses only, re-checked at
  connect time, no redirects, an admin allowlist.
- Per-user (D-150): each capture uses its OWNER's connection — no admin tokens;
  the deprecated TX_* settings seed the deployment owner's connections once.
"""
import logging
import socket
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

KEY1 = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="     # 32 bytes, base64
KEY2 = "ab" * 32                                          # 32 bytes, hex
TOKEN = "at_live_9f8e7d6c5b4a39281706f5e4d3c2b1a0"

if DEPS:
    from app import integrations, netpolicy, secretbox
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, CommitAttempt, TravelItem, User, UserIntegration

    Base.metadata.create_all(engine)
    client = TestClient(app)

    def _clear_ring():
        secretbox._ring_from.cache_clear()

    @pytest.fixture(autouse=True)
    def _env(monkeypatch):
        monkeypatch.setattr(settings, "secrets_key", KEY1)
        monkeypatch.setattr(settings, "secrets_keys", "")
        monkeypatch.setattr(settings, "secrets_key_file", "")
        monkeypatch.setattr(settings, "integration_allowed_hosts", "")
        monkeypatch.setattr(settings, "airtrail_token", "")
        monkeypatch.setattr(settings, "adventurelog_api_key", "")
        monkeypatch.setattr(settings, "airtrail_db_url", "")
        _clear_ring()
        yield
        _clear_ring()
        with SessionLocal() as s:
            ids = [u.id for u in s.query(User).filter(User.username.like("cn-%")).all()]
            s.query(UserIntegration).filter(UserIntegration.user_id.in_(ids)).delete(
                synchronize_session=False)
            s.query(User).filter(User.id.in_(ids)).delete(synchronize_session=False)
            s.commit()

    def _user(admin=False):
        with SessionLocal() as s:
            u = User(username=f"cn-{uuid.uuid4().hex[:8]}", auth_source="local", is_admin=admin)
            s.add(u)
            s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _public_dns(monkeypatch, ip="93.184.216.34"):
        """Every host resolves to `ip` (public by default) — no real DNS."""
        def fake(host, port, *a, **k):
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]
        monkeypatch.setattr(netpolicy.socket, "getaddrinfo", fake)

    def _save(u, kind="airtrail", url="https://at.example.com", token=TOKEN, **kw):
        with SessionLocal() as s:
            c = integrations.save(s, s.get(User, u.id), kind, base_url=url, credential=token, **kw)
            s.commit()
            return c.id

    # ------------------------------------------------------------ secret box (D-151)

    def test_round_trip_is_bound_to_its_row():
        aad = secretbox.aad_for("row-1", "user-1", "airtrail")
        ct = secretbox.encrypt(TOKEN, aad)
        assert ct.startswith("v1:") and TOKEN not in ct
        assert secretbox.decrypt(ct, aad) == TOKEN
        # Copied onto another row (or user, or kind), it doesn't decrypt.
        for other in (("row-2", "user-1", "airtrail"), ("row-1", "user-2", "airtrail"),
                      ("row-1", "user-1", "adventurelog")):
            with pytest.raises(secretbox.SecretError) as err:
                secretbox.decrypt(ct, secretbox.aad_for(*other))
            assert TOKEN not in str(err.value)
        # A fresh nonce each time.
        assert secretbox.encrypt(TOKEN, aad) != ct

    def test_keyring_rotation(monkeypatch):
        aad = secretbox.aad_for("r", "u", "airtrail")
        old = secretbox.encrypt(TOKEN, aad)                          # v1
        monkeypatch.setattr(settings, "secrets_keys", f"v2:{KEY2},v1:{KEY1}")
        _clear_ring()
        assert secretbox.decrypt(old, aad) == TOKEN                  # old key still reads
        new = secretbox.encrypt(TOKEN, aad)
        assert new.startswith("v2:")                                 # newest key writes
        monkeypatch.setattr(settings, "secrets_keys", f"v2:{KEY2}")  # v1 retired
        _clear_ring()
        with pytest.raises(secretbox.SecretError):
            secretbox.decrypt(old, aad)

    def test_rotate_reencrypts_every_row(monkeypatch):
        _public_dns(monkeypatch)
        u = _user()
        cid = _save(u)
        monkeypatch.setattr(settings, "secrets_keys", f"v2:{KEY2},v1:{KEY1}")
        _clear_ring()
        with SessionLocal() as s:
            assert integrations.rotate(s) >= 1
            s.commit()
            c = s.get(UserIntegration, cid)
            assert c.credential_ct.startswith("v2:")
            assert integrations._credential(c) == TOKEN

    def test_no_key_means_no_connections(monkeypatch):
        monkeypatch.setattr(settings, "secrets_key", "")
        _clear_ring()
        assert not secretbox.available() and "TX_SECRETS_KEY" in secretbox.problem()
        u = _user()
        with SessionLocal() as s, pytest.raises(integrations.ConnectionProblem):
            integrations.save(s, s.get(User, u.id), "airtrail",
                              base_url="https://at.example.com", credential=TOKEN)

    def test_malformed_key_is_reported_not_used(monkeypatch):
        monkeypatch.setattr(settings, "secrets_key", "too-short")
        _clear_ring()
        assert not secretbox.available() and secretbox.problem()

    def test_hint_never_gives_a_short_secret_away():
        assert secretbox.hint(TOKEN) == TOKEN[-4:]
        assert secretbox.hint("abc123") == ""

    # ------------------------------------------------------------ URL policy (D-152)

    @pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.3.4",
                                    "169.254.169.254", "100.64.0.1", "::1", "fd00::1",
                                    "::ffff:10.0.0.1", "0.0.0.0"])
    def test_private_and_local_addresses_are_refused(monkeypatch, ip):
        _public_dns(monkeypatch, ip)
        with pytest.raises(netpolicy.BlockedURL):
            netpolicy.check_url("https://looks-public.example.com")

    def test_public_https_is_allowed(monkeypatch):
        _public_dns(monkeypatch)
        assert netpolicy.check_url("https://at.example.com/") == "https://at.example.com"

    @pytest.mark.parametrize("url", ["http://at.example.com", "ftp://at.example.com",
                                     "https://user:pw@at.example.com", "at.example.com",
                                     "https://at.example.com/?x=1"])
    def test_bad_urls_are_refused(monkeypatch, url):
        _public_dns(monkeypatch)
        with pytest.raises(netpolicy.BlockedURL):
            netpolicy.check_url(url)

    def test_admin_allowlist_permits_a_lan_host(monkeypatch):
        _public_dns(monkeypatch, "10.0.0.7")
        monkeypatch.setattr(settings, "integration_allowed_hosts", "airtrail:3000")
        assert netpolicy.check_url("http://airtrail:3000") == "http://airtrail:3000"
        with pytest.raises(netpolicy.BlockedURL):              # other ports aren't
            netpolicy.check_url("http://airtrail:9999")

    def test_connect_time_check_defeats_dns_rebinding(monkeypatch):
        """Saved while it resolved publicly; by the time we connect it points
        at the metadata address. The guarded opener re-checks and refuses."""
        _public_dns(monkeypatch)
        netpolicy.check_url("https://rebind.example.com")
        _public_dns(monkeypatch, "169.254.169.254")
        import urllib.error
        import urllib.request
        with pytest.raises((urllib.error.URLError, netpolicy.BlockedURL)) as err:
            netpolicy.guarded_opener().open(
                urllib.request.Request("https://rebind.example.com/api"), timeout=2)
        assert "private or local" in str(err.value)

    def test_redirects_are_not_followed():
        import urllib.error
        import urllib.request
        h = netpolicy._NoRedirect()
        req = urllib.request.Request("https://a.example.com/x",
                                     headers={"Authorization": "Bearer " + TOKEN})
        with pytest.raises(urllib.error.URLError) as err:
            h.redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/")
        assert TOKEN not in str(err.value)

    # ------------------------------------------------------------ service (D-150)

    def test_saved_credential_is_ciphertext_with_a_hint(monkeypatch):
        _public_dns(monkeypatch)
        u = _user()
        cid = _save(u, remote_user_id="at-user-7")
        with SessionLocal() as s:
            c = s.get(UserIntegration, cid)
            assert TOKEN not in (c.credential_ct or "") and c.credential_hint == TOKEN[-4:]
            assert c.remote_user_id == "at-user-7"
            cl = integrations.client(c)
            assert cl.token == TOKEN and TOKEN not in repr(cl)
            assert cl._open.__self__.__class__.__name__ == "OpenerDirector"   # guarded

    def test_changing_the_url_needs_the_token_again(monkeypatch):
        _public_dns(monkeypatch)
        u = _user()
        _save(u)
        with SessionLocal() as s, pytest.raises(integrations.ConnectionProblem) as err:
            integrations.save(s, s.get(User, u.id), "airtrail",
                              base_url="https://elsewhere.example.com", credential="")
        assert "re-enter the token" in str(err.value)
        # Same URL, no token: keeps the saved one.
        with SessionLocal() as s:
            c = integrations.save(s, s.get(User, u.id), "airtrail",
                                  base_url="https://at.example.com", credential="")
            assert integrations._credential(c) == TOKEN

    def test_no_admin_tokens_and_off_means_off(monkeypatch):
        _public_dns(monkeypatch)
        alice, admin = _user(), _user(admin=True)
        _save(alice)
        with SessionLocal() as s:
            assert integrations.clients_for_user(s, alice.id).airtrail is not None
            # The admin has no connection of their own: nothing, not alice's.
            assert integrations.clients_for_user(s, admin.id).airtrail is None
            c = integrations.get(s, alice.id, "airtrail")
            c.enabled = False
            s.commit()
            assert integrations.clients_for_user(s, alice.id).external == frozenset()

    def test_purge_user_removes_their_ciphertext(monkeypatch):
        _public_dns(monkeypatch)
        u = _user()
        _save(u)
        _save(u, kind="adventurelog", url="https://al.example.com")
        with SessionLocal() as s:
            assert integrations.purge_user(s, u.id) == 2
            s.commit()
            assert integrations.for_user(s, u.id) == {}

    def test_seed_from_env_moves_the_server_wide_settings_to_the_owner(monkeypatch):
        owner = _user(admin=True)
        monkeypatch.setattr(integrations, "owner_user_id", lambda session: owner.id)
        monkeypatch.setattr(settings, "airtrail_url", "http://airtrail:3000")
        monkeypatch.setattr(settings, "airtrail_token", TOKEN)
        monkeypatch.setattr(settings, "adventurelog_url", "http://adventurelog-server:80")
        monkeypatch.setattr(settings, "adventurelog_api_key", "al-key-0123456789abcdef")
        monkeypatch.setattr(settings, "adventurelog_public_url", "https://al.example.com")
        with SessionLocal() as s:
            assert sorted(integrations.seed_from_env(s)) == ["adventurelog", "airtrail"]
            assert integrations.seed_from_env(s) == []                  # once
            conns = integrations.for_user(s, owner.id)
            assert integrations._credential(conns["airtrail"]) == TOKEN
            assert conns["adventurelog"].public_url == "https://al.example.com"
        # The operator's own hosts are trusted while the old settings are set.
        assert netpolicy.is_allowlisted("airtrail", 3000)

    # ------------------------------------------------------------ the page

    def _cookie(u):
        from app import authn
        return {settings.auth_cookie_name: authn.issue_access_token(u)}

    def test_page_never_shows_the_token(monkeypatch):
        _public_dns(monkeypatch)
        monkeypatch.setattr(settings, "auth_required", True)
        monkeypatch.setattr(integrations, "_probe", lambda c: (True, "connected · 3 flights"))
        u = _user()
        r = client.post("/settings/integrations/airtrail", cookies=_cookie(u), data={
            "base_url": "https://at.example.com", "credential": TOKEN})
        assert r.status_code == 200 and "connected · 3 flights" in r.text
        assert TOKEN not in r.text and f"…{TOKEN[-4:]}" in r.text
        page = client.get("/settings/integrations", cookies=_cookie(u)).text
        assert TOKEN not in page and "Connected" in page

    def test_page_refuses_an_unsafe_url(monkeypatch):
        _public_dns(monkeypatch, "10.1.2.3")
        monkeypatch.setattr(settings, "auth_required", True)
        u = _user()
        r = client.post("/settings/integrations/airtrail", cookies=_cookie(u), data={
            "base_url": "https://intranet.example.com", "credential": TOKEN})
        assert "private or local" in r.text
        assert 'value="https://intranet.example.com"' in r.text     # kept for fixing…
        assert TOKEN not in r.text                                   # …but never the token
        with SessionLocal() as s:
            assert integrations.get(s, u.id, "airtrail") is None

    def test_connections_are_your_own(monkeypatch):
        """Scoping: the routes only ever act on the caller's own connection, and
        the admin overview shows who/what/status — never a token."""
        _public_dns(monkeypatch)
        monkeypatch.setattr(settings, "auth_required", True)
        alice, bob, admin = _user(), _user(), _user(admin=True)
        _save(alice)
        # Bob has nothing to test, toggle or delete — alice's isn't reachable.
        for action in ("test", "toggle", "delete"):
            assert client.post(f"/settings/integrations/airtrail/{action}",
                               cookies=_cookie(bob)).status_code == 404
        with SessionLocal() as s:
            c = integrations.get(s, alice.id, "airtrail")
            assert c is not None and c.enabled
        assert "at.example.com" not in client.get("/settings/integrations",
                                                  cookies=_cookie(bob)).text
        admin_page = client.get("/settings/integrations", cookies=_cookie(admin)).text
        assert alice.username in admin_page and TOKEN not in admin_page

    def test_api_tokens_cannot_change_connections(monkeypatch):
        """A connected app or personal API token may use the data, not handle
        the person's credentials (first-party only, like account settings)."""
        from app import oauth
        _public_dns(monkeypatch)
        monkeypatch.setattr(settings, "auth_required", True)
        u = _user()
        with SessionLocal() as s:
            _, pat = oauth.create_pat(s, s.get(User, u.id), "test")
            s.commit()
        r = client.post("/settings/integrations/airtrail",
                        headers={"Authorization": f"Bearer {pat}"},
                        data={"base_url": "https://at.example.com", "credential": TOKEN})
        assert r.status_code == 403

    def test_signed_out_cannot_change_connections(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        r = client.post("/settings/integrations/airtrail",
                        data={"base_url": "https://at.example.com", "credential": TOKEN},
                        follow_redirects=False)
        assert r.status_code in (401, 303, 307)

    def test_settings_hub_lists_connections():
        assert "Connections" in client.get("/settings").text
        assert "Targets configured" not in client.get("/").text     # not on the home page

    # ------------------------------------------------------------ per-owner writes

    class _FakeResp:
        def __init__(self, status, body):
            self.status, self._body = status, body

        def read(self, n=-1):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_commit_uses_the_capture_owners_connection_and_never_leaks_the_token(
            monkeypatch, caplog):
        """An admin logging a sharing user's capture writes to THAT user's
        AdventureLog with THEIR token; the token is in no commit_attempts row and
        no log line."""
        import json

        from app.committing import commit_pending
        _public_dns(monkeypatch)
        alice, admin = _user(), _user(admin=True)
        _save(alice, kind="adventurelog", url="https://al-alice.example.com")
        _save(admin, kind="adventurelog", url="https://al-admin.example.com",
              token="admin-token-0123456789abcdef")
        seen = []

        class _Opener:
            def open(self, req, timeout=None):
                seen.append((req.full_url, req.get_header("X-api-key")))
                return _FakeResp(201, json.dumps({"id": "note-1"}).encode())
        monkeypatch.setattr(netpolicy, "guarded_opener", lambda: _Opener())
        with SessionLocal() as s:
            it = TravelItem(
                item_type="event", schema_version="1.4.0", owner_id=alice.id,
                canonical_key=f"cn-{uuid.uuid4().hex[:6]}",
                source={"type": "manual", "capture_context": {}},
                extraction={"payload": {"item_type": "event", "event": {
                    "name": "Concert", "start": {"local": "2031-05-02T20:00",
                                                 "precision": "datetime"}}}},
                resolution={"outcome": "resolved", "targets": ["adventurelog.note"],
                            "collection_id": "col-a"},
                review={"required": False, "action_taken": "approved"},
                status={"resolve": "ok"})
            s.add(it)
            s.commit()
            iid = it.id
            caplog.set_level(logging.DEBUG)
            commit_pending(s, dry_run=False, item_filter=TravelItem.id == iid,
                           clients_for=integrations.ClientCache(s))
            assert seen and all(url.startswith("https://al-alice.example.com")
                                for url, _ in seen)
            assert {key for _, key in seen} == {TOKEN}             # alice's, not admin's
            for a in s.query(CommitAttempt).filter(CommitAttempt.item_id == iid).all():
                blob = json.dumps([a.request_payload, a.response_headers, a.response_body],
                                  default=str)
                assert TOKEN not in blob
            assert TOKEN not in caplog.text
            s.query(CommitAttempt).filter(CommitAttempt.item_id == iid).delete()
            s.delete(s.get(TravelItem, iid))
            s.commit()
