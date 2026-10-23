"""YONDER-199 — email-forward capture (SPEC-9 D-162, D-190).

The admin's mailbox `trips@…` accepts plus-addressed mail; each user forwards to
their own `trips+<token>@…`. The address decides whose capture it is; mail to an
address that matches no one is dropped and counted, never stored."""
import base64
import imaplib
import json
import sys
import uuid
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
    from app import authn, inbox, scope
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, InboxAddress, InboxMessage, IngestBatch, TravelItem,
                            User)
    from app.processing import process_pending

    Base.metadata.create_all(engine)
    client = TestClient(app)

    MAILBOX = "trips@example.test"
    _LD = json.dumps({
        "@context": "https://schema.org", "@type": "LodgingReservation",
        "reservationNumber": "IN199", "checkinDate": "2031-03-20", "checkoutDate": "2031-03-23",
        "reservationFor": {"@type": "LodgingBusiness", "name": "Inbox Guesthouse",
                           "address": {"@type": "PostalAddress",
                                       "addressLocality": "Addis Ababa",
                                       "addressCountry": "ET"}}})
    HTML = ('<html><head><script type="application/ld+json">'
            f'{_LD}</script></head><body>Your stay</body></html>')

    @pytest.fixture(autouse=True)
    def _inbox_on(monkeypatch):
        monkeypatch.setattr(settings, "inbox_address", MAILBOX)
        monkeypatch.setattr(settings, "inbox_imap_host", "imap.example.test")
        monkeypatch.setattr(settings, "inbox_imap_user", MAILBOX)
        monkeypatch.setattr(settings, "inbox_imap_password", "s3cret-app-pass")
        monkeypatch.setattr(settings, "inbox_rate_limit", 30)
        yield

    def _user(session, name="in"):
        u = User(username=f"{name}-{uuid.uuid4().hex[:8]}", auth_source="local")
        session.add(u)
        session.commit()
        return u

    def _token(address):
        return address.split("+", 1)[1].split("@", 1)[0]

    def _mail(to, *, msgid=None, html=HTML, extra=None) -> bytes:
        m = EmailMessage()
        m["From"] = "Hotel <bookings@hotel.example>"
        m["To"] = to
        m["Subject"] = "Your reservation IN199"
        m["Date"] = "Mon, 12 Oct 2026 10:00:00 +0000"
        m["Message-ID"] = msgid or f"<{uuid.uuid4().hex}@hotel.example>"
        for k, v in (extra or {}).items():
            m[k] = v
        m.set_content("Your stay")
        m.add_alternative(html, subtype="html")
        return bytes(m)

    def _batches_for(session, user_id):
        return session.query(IngestBatch).filter_by(user_id=user_id).all()

    # ------------------------------------------------------------- addresses
    def test_address_is_made_once_and_regenerating_retires_the_old_one():
        with SessionLocal() as s:
            u = _user(s)
            a1 = inbox.user_address(s, u)
            assert a1 == inbox.user_address(s, u)                  # stable
            assert a1.startswith("trips+") and a1.endswith("@example.test")
            a2 = inbox.rotate(s, u)
            assert a2 != a1
            # the old address now matches no one: dropped, nothing stored
            assert inbox.handle_message(s, _mail(a1))[0].outcome == inbox.UNMATCHED
            assert _batches_for(s, u.id) == []
            assert inbox.handle_message(s, _mail(a2))[0].outcome == inbox.CAPTURED

    def test_no_address_when_the_inbox_is_not_set_up(monkeypatch):
        monkeypatch.setattr(settings, "inbox_imap_host", "")
        with SessionLocal() as s:
            assert inbox.user_address(s, _user(s)) is None

    # ------------------------------------------------------------- one message
    def test_forwarded_mail_becomes_the_users_capture():
        with SessionLocal() as s:
            u = _user(s)
            addr = inbox.user_address(s, u)
            raw = _mail(f"Me <{addr}>")
            [h] = inbox.handle_message(s, raw)
            assert h.outcome == inbox.CAPTURED and h.user_id == u.id
            b = s.get(IngestBatch, h.batch_id)
            assert b.user_id == u.id                               # owned, never open
            assert b.payload["capture_channel"] == "email"
            assert base64.b64decode(b.payload["raw_message_base64"]) == raw   # D-15
            process_pending(s, llm=None)
            [item] = s.query(TravelItem).filter_by(batch_id=b.id).all()
            assert item.owner_id == u.id and item.item_type == "lodging"
            assert item.source["type"] == "email"

    def test_delivered_to_counts_when_the_visible_to_is_someone_else():
        """A forwarding rule keeps the original To; the server's Delivered-To
        says where it really went."""
        with SessionLocal() as s:
            u = _user(s)
            addr = inbox.user_address(s, u)
            raw = _mail("someone@elsewhere.example", extra={"Delivered-To": addr.upper()})
            assert inbox.handle_message(s, raw)[0].outcome == inbox.CAPTURED

    def test_unknown_or_bare_address_is_dropped_and_never_stored():
        with SessionLocal() as s:
            before = s.query(IngestBatch).count()
            for to in ("trips+nosuchtoken@example.test", MAILBOX,
                       "trips+abc@other.example"):
                [h] = inbox.handle_message(s, _mail(to))
                assert h.outcome == inbox.UNMATCHED and h.user_id is None
            assert s.query(IngestBatch).count() == before          # YONDER-186
            assert s.query(InboxMessage).filter_by(outcome=inbox.UNMATCHED).count() >= 3

    def test_the_same_message_is_captured_once():
        with SessionLocal() as s:
            u = _user(s)
            raw = _mail(inbox.user_address(s, u), msgid="<same-199@hotel.example>")
            assert inbox.handle_message(s, raw)[0].outcome == inbox.CAPTURED
            assert inbox.handle_message(s, raw)[0].outcome == inbox.DUPLICATE
            assert len(_batches_for(s, u.id)) == 1

    def test_one_message_to_two_people_is_a_capture_each():
        with SessionLocal() as s:
            a, b = _user(s, "ana"), _user(s, "ben")
            raw = _mail(inbox.user_address(s, a),
                        extra={"Cc": inbox.user_address(s, b)})
            outcomes = {h.user_id: h.outcome for h in inbox.handle_message(s, raw)}
            assert outcomes == {a.id: inbox.CAPTURED, b.id: inbox.CAPTURED}

    def test_each_address_is_rate_limited(monkeypatch):
        monkeypatch.setattr(settings, "inbox_rate_limit", 1)
        with SessionLocal() as s:
            u = _user(s)
            addr = inbox.user_address(s, u)
            assert inbox.handle_message(s, _mail(addr))[0].outcome == inbox.CAPTURED
            assert inbox.handle_message(s, _mail(addr))[0].outcome == inbox.RATE_LIMITED
            assert len(_batches_for(s, u.id)) == 1

    def test_too_large_mail_is_dropped(monkeypatch):
        monkeypatch.setattr(settings, "inbox_max_bytes", 200)
        with SessionLocal() as s:
            u = _user(s)
            assert (inbox.handle_message(s, _mail(inbox.user_address(s, u)))[0].outcome
                    == inbox.TOO_LARGE)
            assert _batches_for(s, u.id) == []

    def test_a_deactivated_users_address_is_dropped():
        with SessionLocal() as s:
            u = _user(s)
            addr = inbox.user_address(s, u)
            u.is_active = False
            s.commit()
            assert inbox.handle_message(s, _mail(addr))[0].outcome == inbox.UNMATCHED

    def test_someone_elses_inbox_capture_is_not_visible_to_me():
        """Scoping (house rule 2): Ben's inbox capture is his alone."""
        with SessionLocal() as s:
            ana, ben = _user(s, "ana"), _user(s, "ben")
            [h] = inbox.handle_message(s, _mail(inbox.user_address(s, ben)))
            mine = s.query(IngestBatch).filter(scope.batch_filter(ana)).all()
            assert h.batch_id not in {b.id for b in mine}
            theirs = s.query(IngestBatch).filter(scope.batch_filter(ben)).all()
            assert h.batch_id in {b.id for b in theirs}

    # ------------------------------------------------------------- the IMAP poll
    class FakeIMAP:
        """Just enough of imaplib.IMAP4 for the reader: header-only FETCH over a
        UID range, MOVE / COPY, UID EXPUNGE, and a record of every command."""
        def __init__(self, messages, move=True, uidplus=True, fail_login=None):
            self.messages = dict(messages)            # uid(bytes) -> raw bytes
            caps = ["IMAP4REV1"] + (["MOVE"] if move else []) + (["UIDPLUS"] if uidplus else [])
            self.capabilities = tuple(caps)
            self.moved: dict[bytes, str] = {}
            self.created: list[str] = []
            self.fetched_bodies: list[bytes] = []
            self.header_scans: list[str] = []
            self.plain_expunges = 0
            self.uid_expunged: list[bytes] = []
            self.fail_login = fail_login

        def login(self, user, password):
            if self.fail_login:
                raise imaplib.IMAP4.error(self.fail_login)
            return "OK", [b"logged in"]

        def capability(self):
            return "OK", [" ".join(self.capabilities).encode()]

        def select(self, folder):
            return "OK", [str(len(self.messages)).encode()]

        def response(self, code):
            return code, [b"42"]

        def create(self, folder):
            self.created.append(folder)
            return "NO", [b"exists"]

        def uid(self, cmd, *args):
            import email as _email
            cmd = cmd.upper()
            if cmd == "FETCH":
                uidset, what = args
                if "HEADER.FIELDS" in what:
                    self.header_scans.append(uidset.decode() if isinstance(uidset, bytes) else uidset)
                    start = int(str(uidset).split(":")[0])
                    out = []
                    pick = [u for u in sorted(self.messages, key=int) if int(u) >= start] \
                        or sorted(self.messages, key=int)[-1:]      # n:* answers the last one
                    for u in pick:
                        m = _email.message_from_bytes(self.messages[u])
                        hdr = "".join(f"{k}: {v}\r\n" for k, v in m.items()
                                      if k.lower() in ("to", "cc", "delivered-to",
                                                       "x-original-to")).encode()
                        out += [(f"1 (UID {u.decode()} BODY[HEADER] {{{len(hdr)}}}".encode(), hdr),
                                b")"]
                    return "OK", out
                uid = uidset
                raw = self.messages[uid]
                if "RFC822.SIZE" in what:
                    return "OK", [f"1 (UID {uid.decode()} RFC822.SIZE {len(raw)})".encode()]
                self.fetched_bodies.append(uid)
                return "OK", [(f"1 (UID {uid.decode()} BODY[] {{{len(raw)}}}".encode(), raw),
                              b")"]
            if cmd in ("MOVE", "COPY"):
                uid, folder = args
                self.moved[uid] = folder.strip('"')
                if cmd == "MOVE":
                    self.messages.pop(uid, None)            # it leaves the folder
                return "OK", [b"done"]
            if cmd == "STORE":
                return "OK", [b""]
            if cmd == "EXPUNGE":
                self.uid_expunged.append(args[0])
                self.messages.pop(args[0], None)
                return "OK", [b""]
            raise AssertionError(cmd)

        def expunge(self):
            self.plain_expunges += 1
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    @pytest.fixture(autouse=True)
    def _fresh_scan_marks():
        inbox._scanned.clear()
        yield
        inbox._scanned.clear()

    def test_poll_files_captured_and_dropped_mail_and_starts_processing(monkeypatch):
        with SessionLocal() as s:
            u = _user(s)
            addr = inbox.user_address(s, u)
            uid_ = u.id
        fake = FakeIMAP({b"1": _mail(addr), b"2": _mail("trips+nobody@example.test")})
        monkeypatch.setattr(inbox, "_connect", lambda: fake)
        kicked = []
        from app.review import autoprocess
        monkeypatch.setattr(autoprocess, "kick", lambda user: kicked.append(user.id))
        counts = inbox.poll_once(SessionLocal)
        assert counts == {inbox.CAPTURED: 1, inbox.UNMATCHED: 1}
        assert fake.moved == {b"1": settings.inbox_done_folder,
                              b"2": settings.inbox_dropped_folder}
        assert kicked == [uid_]                                    # D-158, unattended

    def test_without_move_only_our_message_is_expunged(monkeypatch):
        """COPY + flag + UID EXPUNGE of that one message — never a plain EXPUNGE,
        which would also delete anything else flagged in a shared mailbox."""
        with SessionLocal() as s:
            addr = inbox.user_address(s, _user(s))
        from app.review import autoprocess
        monkeypatch.setattr(autoprocess, "kick", lambda user: None)
        fake = FakeIMAP({b"7": _mail(addr)}, move=False)
        monkeypatch.setattr(inbox, "_connect", lambda: fake)
        inbox.poll_once(SessionLocal)
        assert fake.moved == {b"7": settings.inbox_done_folder}
        assert fake.uid_expunged == [b"7"] and fake.plain_expunges == 0
        inbox._scanned.clear()
        bare = FakeIMAP({b"8": _mail("trips+zzzz@example.test")}, move=False, uidplus=False)
        monkeypatch.setattr(inbox, "_connect", lambda: bare)
        inbox.poll_once(SessionLocal)
        assert bare.plain_expunges == 0 and bare.uid_expunged == []   # left flagged instead

    def test_a_personal_mailbox_is_left_alone(monkeypatch):
        """The mailbox may be someone's own (Proton: one account, many addresses).
        Only mail to OUR plus addresses is read, moved or recorded; personal mail
        and mail to the bare address are never fetched, moved or counted."""
        with SessionLocal() as s:
            addr = inbox.user_address(s, _user(s))
        from app.review import autoprocess
        monkeypatch.setattr(autoprocess, "kick", lambda user: None)
        personal = _mail("Kenji <me@pm.example>", msgid="<personal-1@x>")
        bare = _mail(MAILBOX, msgid="<bare-1@x>")
        fake = FakeIMAP({b"1": personal, b"2": _mail(addr), b"3": bare})
        monkeypatch.setattr(inbox, "_connect", lambda: fake)
        with SessionLocal() as s:
            before = s.query(InboxMessage).count()
        counts = inbox.poll_once(SessionLocal)
        assert counts == {inbox.CAPTURED: 1}
        assert set(fake.moved) == {b"2"} and fake.fetched_bodies == [b"2"]
        assert {b"1", b"3"} <= set(fake.messages)                      # untouched, in place
        with SessionLocal() as s:
            assert s.query(InboxMessage).count() == before + 1

    def test_scanned_mail_is_not_rescanned_on_every_wake(monkeypatch):
        from app.review import autoprocess
        monkeypatch.setattr(autoprocess, "kick", lambda user: None)
        fake = FakeIMAP({b"5": _mail("me@pm.example"), b"9": _mail("me@pm.example")})
        monkeypatch.setattr(inbox, "_connect", lambda: fake)
        inbox.poll_once(SessionLocal)
        inbox.poll_once(SessionLocal)
        assert fake.header_scans == ["1:*", "10:*"] and fake.moved == {}

    def test_capabilities_are_reread_after_login(monkeypatch):
        """Bridge lists MOVE/UIDPLUS/IDLE only once signed in."""
        class Conn:
            capabilities = ("IMAP4REV1", "STARTTLS")
            def __init__(self, *a, **kw): pass
            def starttls(self, ssl_context=None): pass
            def login(self, *a): return "OK", []
            def capability(self): return "OK", [b"IMAP4rev1 IDLE MOVE UIDPLUS"]
        monkeypatch.setattr(imaplib, "IMAP4_SSL", Conn)
        conn = inbox._connect()
        assert {"IDLE", "MOVE", "UIDPLUS"} <= set(conn.capabilities)

    def test_a_failed_poll_never_shows_the_password(monkeypatch):
        def boom():
            raise imaplib.IMAP4.error(
                f"LOGIN failed for {settings.inbox_imap_user} / {settings.inbox_imap_password}")
        monkeypatch.setattr(inbox, "_connect", boom)
        inbox.poll_and_record(SessionLocal)
        assert inbox.status["last_error"]
        assert settings.inbox_imap_password not in inbox.status["last_error"]

    # ------------------------------------------------------------- web + API
    def _signed_in(monkeypatch, admin=False):
        monkeypatch.setattr(settings, "auth_required", True)
        with SessionLocal() as s:
            u = _user(s, "web")
            u.is_admin = admin
            s.commit()
            return u.id, {settings.auth_cookie_name: authn.issue_web_token(u)}, \
                {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def test_email_tab_shows_my_address_and_regenerates_it(monkeypatch):
        uid_, cookie, _ = _signed_in(monkeypatch)
        page = client.get("/capture/setup?tab=email", cookies=cookie).text
        with SessionLocal() as s:
            token = s.get(InboxAddress, uid_).token
        assert f"trips+{token}@example.test" in page
        assert "Server mailbox (admins)" not in page               # not an admin
        # YONDER-329: one plain step; forwarding rules are optional and folded away.
        assert "Forward a booking email to the address above." in page
        assert "<summary>Automatic forwarding (optional)</summary>" in page
        assert "update the rule too" in page
        r = client.post("/capture/setup/email-address", cookies=cookie)
        assert r.status_code == 200 and "New address made" in r.text
        with SessionLocal() as s:
            assert s.get(InboxAddress, uid_).token != token

    def test_email_tab_says_when_the_server_has_no_mailbox(monkeypatch):
        _, cookie, _ = _signed_in(monkeypatch, admin=True)
        monkeypatch.setattr(settings, "inbox_imap_host", "")
        page = client.get("/capture/setup?tab=email", cookies=cookie).text
        assert "Not set up on this server yet" in page
        assert "TX_INBOX_IMAP_HOST" in page                         # the admin's how-to
        assert "s3cret-app-pass" not in page

    def test_api_returns_and_rotates_my_address(monkeypatch):
        uid_, _, bearer = _signed_in(monkeypatch)
        r = client.get("/api/v1/capture/email-address", headers=bearer).json()
        assert r["enabled"] is True and _token(r["address"])
        r2 = client.post("/api/v1/capture/email-address/rotate", headers=bearer).json()
        assert r2["address"] != r["address"]
        monkeypatch.setattr(settings, "inbox_imap_host", "")
        r3 = client.get("/api/v1/capture/email-address", headers=bearer).json()
        assert r3 == {"enabled": False, "address": None}

    # ------------------------------------------------------------- "got it" (D-190)
    def _captured_poll(monkeypatch, email=None, smtp=True):
        sent = []
        from app import email as mail
        monkeypatch.setattr(mail, "email_configured", lambda: smtp)
        monkeypatch.setattr(mail, "send_email", lambda to, subj, body: sent.append((to, subj, body)) or True)
        from app.review import autoprocess
        monkeypatch.setattr(autoprocess, "kick", lambda user: None)
        with SessionLocal() as s:
            u = _user(s)
            u.email = email
            s.commit()
            addr = inbox.user_address(s, u)
        raw = _mail(addr, extra={"Reply-To": "attacker@evil.example"})
        monkeypatch.setattr(inbox, "_connect", lambda: FakeIMAP({b"1": raw}))
        inbox.poll_once(SessionLocal)
        return sent

    def test_a_capture_is_acknowledged_to_the_owner_not_the_sender(monkeypatch):
        sent = _captured_poll(monkeypatch, email="owner@example.test")
        assert len(sent) == 1
        to, subject, body = sent[0]
        assert to == "owner@example.test"                 # never bookings@ / Reply-To
        assert subject == "Got it: Your reservation IN199"
        assert "Nothing is filed until you approve it" in body

    def test_no_acknowledgement_without_smtp_or_an_account_email(monkeypatch):
        assert _captured_poll(monkeypatch, email="owner@example.test", smtp=False) == []
        assert _captured_poll(monkeypatch, email=None) == []

    # ------------------------------------------------------------- IDLE push (D-190)
    class IdleConn:
        """The few imaplib internals idle_wait uses, over a real socketpair so
        the select() path is exercised for real."""
        def __init__(self):
            import collections
            import socket
            self.a, self.b = socket.socketpair()
            self.a.setblocking(False)
            self.lines = collections.deque()
            self.sent = []

        def _new_tag(self):
            return b"A001"

        def send(self, data):
            self.sent.append(data)
            if data.endswith(b" IDLE\r\n"):
                self.lines.append(b"+ idling\r\n")
            elif data == b"DONE\r\n":
                self.lines.append(b"A001 OK IDLE terminated\r\n")

        def socket(self):
            return self.a

        def readline(self):
            try:
                self.a.recv(1)
            except BlockingIOError:
                pass
            return self.lines.popleft()

        def push(self, line):
            self.lines.append(line)
            self.b.send(b"x")

    def test_idle_returns_as_soon_as_mail_arrives():
        import threading
        import time
        conn = IdleConn()
        threading.Timer(0.2, conn.push, [b"* 4 EXISTS\r\n"]).start()
        t0 = time.monotonic()
        assert inbox.idle_wait(conn, seconds=30) is True
        assert time.monotonic() - t0 < 5                   # seconds, not a poll interval
        assert conn.sent == [b"A001 IDLE\r\n", b"DONE\r\n"]

    def test_idle_times_out_quietly_and_ends_the_idle():
        conn = IdleConn()
        assert inbox.idle_wait(conn, seconds=0.3) is False
        assert conn.sent[-1] == b"DONE\r\n"

    def test_listen_drains_idles_and_drains_again(monkeypatch):
        with SessionLocal() as s:
            addr = inbox.user_address(s, _user(s))
        fake = FakeIMAP({b"1": _mail(addr)})
        fake.capabilities = ("IMAP4REV1", "MOVE", "IDLE")
        monkeypatch.setattr(inbox, "_connect", lambda: fake)
        from app.review import autoprocess
        monkeypatch.setattr(autoprocess, "kick", lambda user: None)
        calls = []

        def idle(conn, seconds=0):
            calls.append(1)
            if len(calls) == 1:                            # new mail arrives
                fake.messages = {b"2": _mail(addr)}
                return True
            inbox._stop.set()                              # then shut down
            return False
        monkeypatch.setattr(inbox, "idle_wait", idle)
        try:
            assert inbox.listen(SessionLocal) is True
        finally:
            inbox._stop.clear()
        assert set(fake.moved) == {b"1", b"2"} and inbox.status["mode"] == "push"

    def test_listen_falls_back_to_polling_without_idle(monkeypatch):
        fake = FakeIMAP({})
        monkeypatch.setattr(inbox, "_connect", lambda: fake)
        assert inbox.listen(SessionLocal) is False
        assert inbox.status["mode"] == "polling"

    # ------------------------------------------------------------- TLS (YONDER-229)
    def test_the_mailbox_certificate_is_verified_by_default():
        import ssl
        ctx = inbox._tls_context()
        assert inbox.tls_mode() == "verified"
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True

    def test_a_pinned_certificate_is_the_only_one_trusted(monkeypatch, tmp_path):
        """Proton Bridge's self-signed cert: trust exactly that file; its name
        (127.0.0.1) can't match the container's route to Bridge."""
        import ssl
        seen = {}
        real = ssl.create_default_context
        def spy(*a, **kw):
            seen.update(kw)
            return real()                      # no real file needed for the test
        monkeypatch.setattr(ssl, "create_default_context", spy)
        monkeypatch.setattr(settings, "inbox_imap_ca_file", str(tmp_path / "bridge.pem"))
        ctx = inbox._tls_context()
        assert inbox.tls_mode() == "pinned"
        assert seen == {"cafile": str(tmp_path / "bridge.pem")}
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is False

    def test_verification_can_only_be_turned_off_explicitly(monkeypatch):
        import ssl
        monkeypatch.setattr(settings, "inbox_imap_verify", False)
        ctx = inbox._tls_context()
        assert inbox.tls_mode() == "unverified" and ctx.verify_mode == ssl.CERT_NONE

    def test_connect_passes_the_checked_context_both_ways(monkeypatch):
        """Never imaplib's own default context (which checks nothing)."""
        seen = []

        class Conn:
            capabilities = ()
            def __init__(self, *a, **kw):
                seen.append(("ssl", kw.get("ssl_context")))
            def starttls(self, ssl_context=None):
                seen.append(("starttls", ssl_context))
            def login(self, *a):
                return "OK", []
        monkeypatch.setattr(imaplib, "IMAP4_SSL", Conn)
        monkeypatch.setattr(imaplib, "IMAP4", Conn)
        inbox._connect()
        monkeypatch.setattr(settings, "inbox_imap_ssl", False)
        inbox._connect()
        assert seen[0][0] == "ssl" and seen[0][1] is not None
        assert seen[-1][0] == "starttls" and seen[-1][1] is not None
