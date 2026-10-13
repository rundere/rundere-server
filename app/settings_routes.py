"""Settings hub (4.1): one place for everything that isn't daily use.

The top bar keeps only the daily surfaces (Home, Review, Capture, Trips,
Journal); every profile and administration page is an entry here instead of
its own header link. Administration entries show only to admins, or to everyone when
auth is off (the single-user world, same rule as app/scope.py).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from fastapi.templating import Jinja2Templates

from app import scope
from app.db import get_session

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))
from app.icons import install as _install_icons  # noqa: E402
_install_icons(templates)

# The menu itself (entries, names, who sees which group) lives in ONE place,
# review/templates/_settings_menu.html, shared with the Settings sidebar that
# every settings page shows, so the two can never drift apart.


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", {
        "me": getattr(request.state, "user", None)})


# ------------------------------------------------------------------ connections
# Per-user AirTrail / AdventureLog connections (SPEC-8 D-150–D-152, YONDER-192).
# Run·dere is first-party (D-146) and works with neither; these are Advanced.
# Each person sets up their own; a saved token is never shown again (D-151).

def _connections_ctx(session: Session, user, *, msg: str = "", err: str = "",
                     kind: str = "", form: dict | None = None) -> dict:
    from app import integrations, netpolicy, secretbox
    conns = integrations.for_user(session, getattr(user, "id", None))
    cards = []
    for k in integrations.KINDS:
        c = conns.get(k)
        cards.append({
            "kind": k, "label": integrations.LABELS[k], "c": c,
            "what": ("Collections, and the trip page's sync with AdventureLog."
                     if k == integrations.ADVENTURELOG else
                     "Your flights, logged to your AirTrail as well."),
            "msg": msg if k == kind else "", "err": err if k == kind else "",
            # What they typed, back in the form after an error — never the token.
            "form": form if k == kind else None})
    admin = scope.unrestricted(user) or scope.is_admin(user)
    return {
        "me": user, "cards": cards, "signed_in": user is not None,
        "key_problem": secretbox.problem(),
        "admin": admin,
        "overview": integrations.admin_overview(session) if admin else [],
        "deprecated": integrations.deprecated_env() if admin else [],
        "allowed_hosts": sorted(netpolicy.allowed_hosts()) if admin else [],
    }


@router.get("/settings/integrations", response_class=HTMLResponse)
def integrations_page(request: Request, session: Session = Depends(get_session)):
    """Your connections. Shows a saved token only as "ends in …1234"."""
    return templates.TemplateResponse(request, "integrations.html", _connections_ctx(
        session, scope.user_of(request)))


@router.get("/settings/integrations/guide", response_class=HTMLResponse)
def integrations_guide(request: Request):
    """How to create the tokens and connect (D-150, D-186)."""
    return templates.TemplateResponse(request, "integrations_guide.html",
                                      {"me": scope.user_of(request)})


def _connections_user(request: Request, session: Session):
    """The signed-in person changing their own connections, from Run·dere itself:
    a connected app or agent token may use their data but never handle their
    credentials (like the account settings, YONDER-101)."""
    from fastapi import HTTPException

    from app import authn
    from app.models import User
    user = scope.user_of(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in to set up connections")
    if not authn.is_first_party(request):
        raise HTTPException(status_code=403, detail="only Run·dere itself can change connections")
    return session.get(User, user.id)


def _page(request: Request, session: Session, user, kind: str, *, msg="", err="",
          form: dict | None = None):
    return templates.TemplateResponse(request, "integrations.html", _connections_ctx(
        session, user, msg=msg, err=err, kind=kind, form=form))


@router.post("/settings/integrations/{kind}", response_class=HTMLResponse)
def save_connection(kind: str, request: Request, base_url: str = Form(""),
                    credential: str = Form(""), public_url: str = Form(""),
                    remote_user_id: str = Form(""),
                    session: Session = Depends(get_session)):
    from app import integrations
    user = _connections_user(request, session)
    if kind not in integrations.KINDS:
        return HTMLResponse("", status_code=404)
    try:
        c = integrations.save(session, user, kind, base_url=base_url, credential=credential,
                              public_url=public_url, remote_user_id=remote_user_id)
    except integrations.ConnectionProblem as problem:
        session.rollback()
        return _page(request, session, user, kind, err=str(problem),
                     form={"base_url": base_url, "public_url": public_url,
                           "remote_user_id": remote_user_id})
    ok, note = integrations.check(session, c)
    session.commit()
    return _page(request, session, user, kind, msg=f"Saved · {note}" if ok else "",
                 err="" if ok else f"Saved, but the test failed: {note}")


@router.post("/settings/integrations/{kind}/test", response_class=HTMLResponse)
def test_connection(kind: str, request: Request, session: Session = Depends(get_session)):
    from app import integrations
    user = _connections_user(request, session)
    c = integrations.get(session, user.id, kind)
    if c is None:
        return HTMLResponse("", status_code=404)
    ok, note = integrations.check(session, c)
    session.commit()
    return _page(request, session, user, kind, msg=note if ok else "", err="" if ok else note)


@router.post("/settings/integrations/{kind}/toggle", response_class=HTMLResponse)
def toggle_connection(kind: str, request: Request, session: Session = Depends(get_session)):
    from app import integrations
    user = _connections_user(request, session)
    c = integrations.get(session, user.id, kind)
    if c is None:
        return HTMLResponse("", status_code=404)
    c.enabled = not c.enabled
    session.commit()
    return _page(request, session, user, kind,
                 msg="Turned on." if c.enabled else "Turned off — nothing is sent to it.")


@router.post("/settings/integrations/{kind}/delete", response_class=HTMLResponse)
def delete_connection(kind: str, request: Request, session: Session = Depends(get_session)):
    from app import integrations
    user = _connections_user(request, session)
    c = integrations.get(session, user.id, kind)
    if c is None:
        return HTMLResponse("", status_code=404)
    integrations.delete(session, c)
    session.commit()
    return _page(request, session, user, kind,
                 msg="Removed. The saved token is deleted; revoke it in "
                     f"{integrations.LABELS[kind]} too if you no longer need it.")


# --------------------------------------------------------------------------- server mail
# Outgoing (SMTP: reset links, "Got it" emails) and incoming (the email-forward
# inbox) in one admin page, each with its status and a check (owner, 2026-10-12):
# an email that silently never arrives was only discoverable from the logs.
def _mail_context(session: Session, user) -> dict:
    from app import email as mail, inbox
    from app.capture import _ago
    from app.config import settings
    out = {"smtp": {"configured": mail.email_configured(), "summary": mail.smtp_summary(),
                    "last_at": _ago(mail.status["last_at"]), "last_ok": mail.status["last_ok"],
                    "last_kind": mail.status["last_kind"], "last_error": mail.status["last_error"]},
           "inbox": None, "me": user}
    if inbox.configured():
        out["inbox"] = {"mailbox": settings.inbox_address, "host": settings.inbox_imap_host,
                        "port": settings.inbox_imap_port, "folder": settings.inbox_folder,
                        "done": settings.inbox_done_folder,
                        "dropped": settings.inbox_dropped_folder,
                        "mode": inbox.status.get("mode"), "tls": inbox.tls_mode(),
                        "every": max(30, settings.inbox_poll_seconds),
                        "last_read": _ago(inbox.status["last_poll_at"]),
                        "last_error": inbox.status["last_error"],
                        "week": inbox.recent_counts(session)}
    return out


def _mail_page(request: Request, session: Session, user, **flash) -> HTMLResponse:
    return templates.TemplateResponse(request, "server_mail.html",
                                      {**_mail_context(session, user), **flash})


@router.get("/settings/mail", response_class=HTMLResponse)
def server_mail(request: Request, session: Session = Depends(get_session)):
    from app import authn
    return _mail_page(request, session, authn.require_admin(request))


@router.post("/settings/mail/check-smtp", response_class=HTMLResponse)
def server_mail_check_smtp(request: Request, session: Session = Depends(get_session)):
    """Connect, secure and sign in to the SMTP server — sends nothing."""
    from app import authn, email as mail
    user = authn.require_admin(request)
    ok, note = mail.check_connection()
    return _mail_page(request, session, user, smtp_msg=note if ok else "",
                      smtp_err="" if ok else note)


@router.post("/settings/mail/test", response_class=HTMLResponse)
def server_mail_test(request: Request, to: str = Form(""),
                     session: Session = Depends(get_session)):
    """Send a real test message (to the admin's own address by default)."""
    from app import authn, email as mail
    user = authn.require_admin(request)
    to = to.strip() or (user.email or "")
    if not mail.email_configured() or not to:
        return _mail_page(request, session, user,
                          smtp_err="Outgoing email isn't configured." if to
                          else "Enter an address to send the test to.")
    try:
        mail.deliver(to, "Run·dere test email",
                     "This is a test from Run·dere. Password-reset links and \"Got it\" "
                     "emails for forwarded bookings arrive the same way.")
    except Exception as ex:
        return _mail_page(request, session, user, smtp_err=f"Test failed: {mail.explain(ex)}")
    return _mail_page(request, session, user, smtp_msg=f"Test email sent to {to}.")


@router.post("/settings/mail/check-inbox", response_class=HTMLResponse)
def server_mail_check_inbox(request: Request, session: Session = Depends(get_session)):
    """Connect, sign in and open the inbox folder — reads and moves nothing."""
    from app import authn, inbox
    from app.config import settings
    user = authn.require_admin(request)
    if not inbox.configured():
        return _mail_page(request, session, user, inbox_err="The inbox isn't configured.")
    try:
        conn = inbox._connect()
        try:
            typ, data = conn.select(inbox._quote(settings.inbox_folder), readonly=True)
            if typ != "OK":
                raise RuntimeError(f"can't open folder {settings.inbox_folder!r}")
            n = int((data or [b"0"])[0] or 0)
            idle = "IDLE" in (conn.capabilities or ())
        finally:
            inbox._logout(conn)
    except Exception as ex:
        text = f"{type(ex).__name__}: {ex}"
        if settings.inbox_imap_password:
            text = text.replace(settings.inbox_imap_password, "***")
        return _mail_page(request, session, user, inbox_err=text[:300])
    return _mail_page(request, session, user, inbox_msg=(
        f"Connected and signed in; {settings.inbox_folder} holds {n} message"
        f"{'' if n == 1 else 's'}. The server {'offers' if idle else 'does not offer'} "
        f"IMAP IDLE ({'instant' if idle else 'polled'} reading)."))
