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
