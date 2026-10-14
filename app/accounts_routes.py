"""Account onboarding & management (Yonder 4.0 Phase 1a.1 — SPEC-4 D-117).

- **/setup**   first-run: create the initial admin while the users table is
               empty (always reachable then; 404s once any user exists).
- **/register** self-service sign-up, only when TX_AUTH_REGISTRATION_OPEN.
- **/forgot**, **/reset/{token}** — password reset via a one-time link (emailed
  when SMTP is configured; otherwise an admin hands over the link).
- **/account** your own account: change your password, and set/change the
               email that reset links go to (both need your current password);
               choose whether to share your data with admins (4.1).
- **/account** also lists Connected apps (OAuth grants, YONDER-107) with
               revoke, and personal API tokens (create / show once / revoke).
- **/account/sharing** that choice; self-registered / SSO users are asked it
               at first sign-in, together with which traveller they are when
               the account has none (YONDER-145).
- **/account/traveller** link an unlinked account to an unclaimed traveller
               (or a new one) without waiting for an admin.
- **/users**   Administer → Users: admin creates accounts, sets/【resets】
               passwords, toggles admin/active, and links a Traveller
               (pick existing OR create new).

Every account maps to a TravelerIdentity (the self/guest roster) — one identity
per person, which Phase 2 trip-sharing builds on.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import authn
from app.config import settings
from app.db import get_session
from app.email import deliver, email_configured, send_email, smtp_summary
from app.models import AccountEvent, TravelerIdentity, User

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))
from app.icons import install as _install_icons  # noqa: E402
_install_icons(templates)


def _now():
    return datetime.now(timezone.utc)


def _travelers(session: Session) -> list[TravelerIdentity]:
    """The de-duplicated traveller roster surfaced to the admin for user↔traveller
    linking (D-124) — the ONLY user-entered item an admin sees in the UI. Names
    are already unique in the schema; generic placeholder guests (roster stand-ins,
    not real people) are excluded from selection."""
    return (session.query(TravelerIdentity)
            .filter(TravelerIdentity.generic.is_(False))
            .order_by(TravelerIdentity.display_name).all())


def _unclaimed_travelers(session: Session) -> list[TravelerIdentity]:
    """Roster people no account is linked to yet — the only ones a user may
    claim for themselves (YONDER-145); claiming someone else's traveller would
    hand over their profiles."""
    claimed = {tid for (tid,) in session.query(User.traveler_id)
               .filter(User.traveler_id.isnot(None))}
    return [t for t in _travelers(session) if t.id not in claimed]


def _claim_traveler(session: Session, user: User, traveler_id: str,
                    new_name: str) -> str:
    """Link an unlinked user to an unclaimed traveller, or a new one named
    `new_name`. Leaving both empty skips it. Returns an error message or ""."""
    new_name = (new_name or "").strip()
    if user.traveler_id is not None or (not new_name and traveler_id in ("", "none")):
        return ""
    if new_name:
        if session.query(TravelerIdentity).filter(
                func.lower(TravelerIdentity.display_name) == new_name.lower()).first():
            return (f"There's already a traveller called {new_name} — pick them "
                    "from the list if that's you, or use a different name.")
        tid = authn.resolve_traveler(session, traveler_id=None, new_name=new_name)
    else:
        try:
            tid = int(traveler_id)
        except ValueError:
            return "Pick a traveller from the list."
        if tid not in {t.id for t in _unclaimed_travelers(session)}:
            return "That traveller is already linked to another account."
    user.traveler_id = tid
    session.commit()
    return ""


def _sso_only(request: Request):
    """On an SSO-only host (YONDER-164) nobody signs up or asks for a password
    reset — those bounce to the SSO sign-in. A reset LINK (/reset/<token>) still
    works there: reset emails are built from TX_PUBLIC_BASE_URL, often that same
    public host, and the new password is then used on a non-forced host."""
    from app import oidc
    if oidc.forced_on(request):
        return RedirectResponse("/login", status_code=303)
    return None


def _base_url(request: Request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


# --------------------------------------------------------------------------- sharing (4.1)
def opt_out_blocker(user: User) -> str:
    """Why this user can't opt out of sharing yet ("" = they can). A local
    account needs SMTP + its own email first: once private, a password reset
    must reach the user directly, never pass through an admin."""
    if user.auth_source != "local":
        return ""                      # password lives with the SSO provider
    if not email_configured():
        return ("Email isn't set up on this server yet, so a password reset would "
                "have to go through an admin. Ask the admin to configure email.")
    if not user.email:
        return "Add your email address first, so password resets can reach you directly."
    return ""


def _audit(session: Session, user: User, actor: User | None, action: str,
           detail: str = "") -> None:
    session.add(AccountEvent(user_id=user.id, actor_id=actor.id if actor else None,
                             action=action, detail=(detail or None)))


def _notify_private(user: User, what: str) -> None:
    """Tell a user who opted out of sharing that an admin acted on their account."""
    if user.share_with_admins or not user.email:
        return
    send_email(user.email, "An admin changed your Run·dere account",
               f"An administrator {what} on your Run·dere account "
               f"({user.username}).\n\nIf you didn't expect this, contact them.")


def _set_sharing(session: Session, user: User, share: bool) -> str:
    """Apply a sharing choice; returns an error message or ""."""
    if not share:
        blocker = opt_out_blocker(user)
        if blocker:
            return blocker
    changed = bool(user.share_with_admins) != share
    user.share_with_admins = share
    user.sharing_prompt = False
    if changed:
        _audit(session, user, user, "sharing_on" if share else "sharing_off")
    session.commit()
    return ""


def _login_user(response, user: User):
    authn.set_session_cookie(response, authn.issue_web_token(user))
    return response


# --------------------------------------------------------------------------- first-run setup
@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, session: Session = Depends(get_session)):
    if authn.user_count(session) > 0:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html",
                                      {"travelers": _travelers(session)})


@router.post("/setup")
def setup_submit(request: Request, username: str = Form(...), password: str = Form(...),
                 email: str = Form(""), traveler_id: str = Form("none"),
                 new_traveler: str = Form(""), session: Session = Depends(get_session)):
    if authn.user_count(session) > 0:
        return RedirectResponse("/login", status_code=303)
    # The first traveller is the account holder → 'self' if a brand-new one.
    tid = authn.resolve_traveler(session, traveler_id=traveler_id,
                                 new_name=new_traveler, kind="self")
    user = authn.create_user(session, username, password, email=email or None,
                             is_admin=True, traveler_id=tid)
    resp = RedirectResponse("/", status_code=303)
    return _login_user(resp, user)


# --------------------------------------------------------------------------- self-service register
@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request, session: Session = Depends(get_session)):
    if (blocked := _sso_only(request)) is not None:
        return blocked
    if not settings.auth_registration_open:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "register.html",
                                      {"travelers": _travelers(session)})


@router.post("/register")
def register_submit(request: Request, username: str = Form(...), password: str = Form(...),
                    email: str = Form(""), traveler_id: str = Form("none"),
                    new_traveler: str = Form(""), session: Session = Depends(get_session)):
    if (blocked := _sso_only(request)) is not None:
        return blocked
    if not settings.auth_registration_open:
        return RedirectResponse("/login", status_code=303)
    if session.query(User).filter_by(username=username.strip()).one_or_none():
        return templates.TemplateResponse(
            request, "register.html",
            {"travelers": _travelers(session), "error": "That username is taken.",
             "username": username}, status_code=409)
    if email.strip() and authn.email_in_use(session, email):
        return templates.TemplateResponse(
            request, "register.html",
            {"travelers": _travelers(session),
             "error": "That email is already used by another account.",
             "username": username}, status_code=409)
    tid = authn.resolve_traveler(session, traveler_id=traveler_id, new_name=new_traveler)
    user = authn.create_user(session, username, password, email=email or None,
                             is_admin=False, traveler_id=tid, sharing_prompt=True)
    resp = RedirectResponse("/", status_code=303)
    return _login_user(resp, user)


# --------------------------------------------------------------------------- forgot / reset
@router.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    if (blocked := _sso_only(request)) is not None:
        return blocked
    return templates.TemplateResponse(request, "forgot.html",
                                      {"smtp": email_configured()})


@router.post("/forgot", response_class=HTMLResponse)
def forgot_submit(request: Request, identifier: str = Form(...),
                  session: Session = Depends(get_session)):
    if (blocked := _sso_only(request)) is not None:
        return blocked
    ident = identifier.strip()
    user = (session.query(User).filter_by(username=ident).one_or_none()
            or session.query(User).filter(
                func.lower(User.email) == ident.lower()).first())
    # Neutral response regardless — never reveal whether an account exists.
    if user is not None and user.auth_source == "local":
        token = authn.create_reset_token(session, user)
        if user.email and email_configured():
            link = f"{_base_url(request)}/reset/{token}"
            send_email(user.email, "Reset your Run·dere password",
                       f"Use this link to set a new password (valid ~1 hour):\n\n{link}\n\n"
                       "If you didn't request this, ignore this email.")
    return templates.TemplateResponse(request, "forgot.html",
                                      {"smtp": email_configured(), "sent": True})


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_form(request: Request, token: str, session: Session = Depends(get_session)):
    user = authn.peek_reset_token(session, token)
    return templates.TemplateResponse(request, "reset.html",
                                      {"token": token, "valid": user is not None,
                                       "username": user.username if user else None})


@router.post("/reset/{token}")
def reset_submit(request: Request, token: str, password: str = Form(...),
                 confirm: str = Form(...), session: Session = Depends(get_session)):
    if password != confirm:
        return templates.TemplateResponse(
            request, "reset.html",
            {"token": token, "valid": True, "error": "Passwords did not match."},
            status_code=400)
    user = authn.consume_reset_token(session, token, password)
    if user is None:
        return templates.TemplateResponse(
            request, "reset.html", {"token": token, "valid": False}, status_code=400)
    return RedirectResponse("/login?msg=reset", status_code=303)


# --------------------------------------------------------------------------- your account
def _account_ctx(session: Session, user: User, **extra) -> dict:
    from app import oauth
    return {"u": user, "smtp": email_configured(), "blocker": opt_out_blocker(user),
            "apps": oauth.connected_apps(session, user),
            "pats": oauth.list_pats(session, user), "msg": "", "err": "", **extra}


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, msg: str = "", err: str = "",
                 user: User = Depends(authn.current_user),
                 session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "account.html",
                                      _account_ctx(session, user, msg=msg, err=err))


# --------------------------------------------------------------------------- connected apps + API tokens (YONDER-107)
@router.post("/account/connected-apps/{grant_id}/revoke")
def account_revoke_app(grant_id: str, next: str = Form(""),
                       user: User = Depends(authn.current_user),
                       session: Session = Depends(get_session)):
    from app import oauth
    from app.models import OAuthGrant
    # Back to the Capture guide it was disconnected from (YONDER-257) — only
    # ever a page of ours, never an outside address.
    back = next if next.startswith("/capture/setup?tab=") else "/account"
    sep = "&" if "?" in back else "?"
    grant = session.get(OAuthGrant, grant_id)
    if grant is None or grant.user_id != user.id:
        return RedirectResponse(f"{back}{sep}err=No+such+connected+app", status_code=303)
    oauth.revoke_grant(session, grant, actor_id=user.id)
    return RedirectResponse(f"{back}{sep}msg=Disconnected", status_code=303)


@router.post("/account/tokens", response_class=HTMLResponse)
def account_create_token(request: Request, name: str = Form(""), days: str = Form(""),
                         user: User = Depends(authn.current_user),
                         session: Session = Depends(get_session)):
    """Create a personal API token. The value is rendered once, in this
    response — never put in a redirect URL, never stored in the clear."""
    from app import oauth
    try:
        n_days = int(days) if days.strip() else None
    except ValueError:
        n_days = None
    if n_days is not None and not (1 <= n_days <= 3650):
        return RedirectResponse("/account?err=Expiry+must+be+1-3650+days", status_code=303)
    user = session.get(User, user.id)
    row, opaque = oauth.create_pat(session, user, name, n_days)
    resp = templates.TemplateResponse(request, "account.html", _account_ctx(
        session, user, new_token=opaque, new_token_name=row.name,
        msg="Token created — copy it now, it won't be shown again."))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/account/tokens/{token_id}/revoke")
def account_revoke_token(token_id: str, user: User = Depends(authn.current_user),
                         session: Session = Depends(get_session)):
    from app import oauth
    if not oauth.revoke_pat(session, user, token_id):
        return RedirectResponse("/account?err=No+such+token", status_code=303)
    return RedirectResponse("/account?msg=Token+revoked", status_code=303)


@router.post("/account/email")
def account_set_email(email: str = Form(""), current_password: str = Form(""),
                      user: User = Depends(authn.current_user),
                      session: Session = Depends(get_session)):
    user = session.get(User, user.id)
    # Reset links go to this address, so changing it is as sensitive as the
    # password itself: a hijacked session must not be able to redirect them.
    if user.auth_source == "local" and not authn.verify_password(
            current_password, user.password_hash):
        return RedirectResponse("/account?err=Current+password+is+incorrect",
                                status_code=303)
    if (not email.strip() and not user.share_with_admins
            and user.auth_source == "local"):
        return RedirectResponse(
            "/account?err=" + quote_plus("While you're not sharing with admins, your "
                                         "email is how password resets reach you — "
                                         "change it, don't remove it."), status_code=303)
    error = authn.set_user_email(session, user, email)
    if error:
        return RedirectResponse(f"/account?err={quote_plus(error)}", status_code=303)
    return RedirectResponse("/account?msg=Email+saved", status_code=303)


@router.post("/account/password")
def account_set_password(current_password: str = Form(""), password: str = Form(""),
                         confirm: str = Form(""),
                         user: User = Depends(authn.current_user),
                         session: Session = Depends(get_session)):
    user = session.get(User, user.id)
    if user.auth_source != "local":
        return RedirectResponse("/account?err=Your+password+is+managed+by+your+sign-in+provider",
                                status_code=303)
    if not authn.verify_password(current_password, user.password_hash):
        return RedirectResponse("/account?err=Current+password+is+incorrect",
                                status_code=303)
    if not password or password != confirm:
        return RedirectResponse("/account?err=New+passwords+did+not+match",
                                status_code=303)
    user.password_hash = authn.hash_password(password)
    user.token_version += 1           # sign out every other session…
    session.commit()
    resp = RedirectResponse("/account?msg=Password+changed", status_code=303)
    return _login_user(resp, user)    # …but keep this one signed in


@router.get("/account/sharing", response_class=HTMLResponse)
def sharing_prompt_page(request: Request, next: str = "/",
                        user: User = Depends(authn.current_user),
                        session: Session = Depends(get_session)):
    # A self-registered/SSO account with no traveller also says who it is here
    # (YONDER-145) — otherwise its profile pages dead-end at "ask an admin".
    travelers = _unclaimed_travelers(session) if user.traveler_id is None else None
    return templates.TemplateResponse(request, "sharing.html", {
        "u": user, "next": _safe_next(next), "blocker": opt_out_blocker(user),
        "travelers": travelers})


@router.post("/account/sharing")
def sharing_choose(choice: str = Form(""), next: str = Form("/account"),
                   traveler_id: str = Form("none"), new_traveler: str = Form(""),
                   user: User = Depends(authn.current_user),
                   session: Session = Depends(get_session)):
    user = session.get(User, user.id)
    error = _claim_traveler(session, user, traveler_id, new_traveler)
    if error:
        dest = f"/account/sharing?next={quote_plus(_safe_next(next))}&err={quote_plus(error)}"
        return RedirectResponse(dest, status_code=303)
    error = _set_sharing(session, user, choice != "private")
    dest = _safe_next(next)
    if error:
        sep = "&" if "?" in dest else "?"
        if dest.startswith("/account/sharing"):
            return RedirectResponse(f"/account/sharing?err={quote_plus(error)}", status_code=303)
        return RedirectResponse(f"{dest}{sep}err={quote_plus(error)}", status_code=303)
    return RedirectResponse(dest, status_code=303)


@router.get("/account/traveller", response_class=HTMLResponse)
def traveller_page(request: Request, next: str = "/account",
                   user: User = Depends(authn.current_user),
                   session: Session = Depends(get_session)):
    """Self-service traveller link for an unlinked account (YONDER-145)."""
    if user.traveler_id is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "traveller.html", {
        "next": _safe_next(next), "travelers": _unclaimed_travelers(session)})


@router.post("/account/traveller")
def traveller_link(next: str = Form("/account"), traveler_id: str = Form("none"),
                   new_traveler: str = Form(""),
                   user: User = Depends(authn.current_user),
                   session: Session = Depends(get_session)):
    user = session.get(User, user.id)
    error = _claim_traveler(session, user, traveler_id, new_traveler)
    if error:
        return RedirectResponse(f"/account/traveller?next={quote_plus(_safe_next(next))}"
                                f"&err={quote_plus(error)}", status_code=303)
    return RedirectResponse(_safe_next(next), status_code=303)


def _safe_next(nxt: str) -> str:
    """Only same-site paths (no scheme/host) — never an open redirect."""
    nxt = (nxt or "/").strip()
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"


# --------------------------------------------------------------------------- admin: Users
@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, msg: str = "", err: str = "", link: str = "",
               link_for: str = "", admin: User = Depends(authn.require_admin),
               session: Session = Depends(get_session)):
    users = session.query(User).order_by(User.username).all()
    tmap = {t.id: t for t in _travelers(session)}
    return templates.TemplateResponse(request, "users.html", {
        "users": users, "travelers": _travelers(session), "tmap": tmap,
        "me": admin, "registration_open": settings.auth_registration_open,
        "smtp": email_configured(), "smtp_summary": smtp_summary(),
        "msg": msg, "err": err, "link": link, "link_for": link_for,
        "default_quota_mb": settings.document_quota_mb})


def _private(user: User, admin: User) -> bool:
    """Opted out of sharing, and it's someone else's account."""
    return not user.share_with_admins and user.id != admin.id


def _u(session, user_id: str) -> User | None:
    return session.get(User, user_id)


@router.post("/users/create")
def users_create(request: Request, username: str = Form(...), password: str = Form(...),
                 email: str = Form(""), is_admin: str = Form(""),
                 traveler_id: str = Form("none"), new_traveler: str = Form(""),
                 admin: User = Depends(authn.require_admin),
                 session: Session = Depends(get_session)):
    if session.query(User).filter_by(username=username.strip()).one_or_none():
        return RedirectResponse("/users?err=That+username+is+taken", status_code=303)
    if email.strip() and authn.email_in_use(session, email):
        return RedirectResponse("/users?err=That+email+is+already+in+use", status_code=303)
    tid = authn.resolve_traveler(session, traveler_id=traveler_id, new_name=new_traveler)
    authn.create_user(session, username, password, email=email or None,
                      is_admin=bool(is_admin), traveler_id=tid)
    return RedirectResponse(f"/users?msg=Created+{username.strip()}", status_code=303)


@router.post("/users/{user_id}/password")
def users_set_password(user_id: str, password: str = Form(...),
                       admin: User = Depends(authn.require_admin),
                       session: Session = Depends(get_session)):
    user = _u(session, user_id)
    if user is None:
        return RedirectResponse("/users?err=No+such+user", status_code=303)
    if _private(user, admin):
        return RedirectResponse(
            "/users?err=" + quote_plus(f"{user.username} doesn't share with admins, so an "
                                       "admin can't set their password — send a reset "
                                       "link (it's emailed to them)."), status_code=303)
    user.password_hash = authn.hash_password(password)
    user.token_version += 1
    _audit(session, user, admin, "password_set")
    session.commit()
    return RedirectResponse(f"/users?msg=Password+set+for+{user.username}", status_code=303)


@router.post("/users/test-email")
def users_test_email(to: str = Form(""), admin: User = Depends(authn.require_admin)):
    """Send a test message so SMTP misconfiguration shows up here, not as a
    reset email that silently never arrives. Admin-only, so the real error is
    shown."""
    to = to.strip() or (admin.email or "")
    if not email_configured():
        return RedirectResponse("/users?err=SMTP+is+not+configured+(TX_SMTP_*)",
                                status_code=303)
    if not to:
        return RedirectResponse("/users?err=Enter+an+address+to+send+the+test+to",
                                status_code=303)
    try:
        deliver(to, "Run·dere test email",
                "This is a test from Run·dere. Password-reset emails will arrive "
                "the same way.")
    except Exception as e:
        from app.email import explain
        return RedirectResponse(f"/users?err={quote_plus('Test email failed: ' + explain(e))}",
                                status_code=303)
    return RedirectResponse(f"/users?msg={quote_plus(f'Test email sent to {to}')}",
                            status_code=303)


@router.post("/users/{user_id}/email")
def users_set_email(user_id: str, email: str = Form(""),
                    admin: User = Depends(authn.require_admin),
                    session: Session = Depends(get_session)):
    user = _u(session, user_id)
    if user is None:
        return RedirectResponse("/users?err=No+such+user", status_code=303)
    if _private(user, admin):
        return RedirectResponse(
            "/users?err=" + quote_plus(f"{user.username} doesn't share with admins, so only "
                                       "they can change their email."), status_code=303)
    error = authn.set_user_email(session, user, email)
    if error:
        return RedirectResponse(f"/users?err={quote_plus(error)}", status_code=303)
    _audit(session, user, admin, "email_set")
    session.commit()
    return RedirectResponse(f"/users?msg=Email+updated+for+{user.username}", status_code=303)


@router.post("/users/{user_id}/quota")
def users_set_quota(user_id: str, quota_mb: str = Form(""),
                    admin: User = Depends(authn.require_admin),
                    session: Session = Depends(get_session)):
    """An admin raises (or resets) one person's file-storage quota (SPEC-8 D-196).
    The quota is the server's room, not the person's data, so it's settable
    whatever their admin-sharing choice; blank = the deployment default."""
    user = _u(session, user_id)
    if user is None:
        return RedirectResponse("/users?err=No+such+user", status_code=303)
    raw = (quota_mb or "").strip()
    if raw and (not raw.isdigit() or not 1 <= int(raw) <= 1_000_000):
        return RedirectResponse("/users?err=" + quote_plus("Quota is a whole number of MB"),
                                status_code=303)
    user.document_quota_mb = int(raw) if raw else None
    _audit(session, user, admin, "document_quota_set")
    session.commit()
    return RedirectResponse(f"/users?msg=File+quota+updated+for+{user.username}",
                            status_code=303)


@router.post("/users/{user_id}/reset-link")
def users_reset_link(request: Request, user_id: str,
                     admin: User = Depends(authn.require_admin),
                     session: Session = Depends(get_session)):
    user = _u(session, user_id)
    if user is None:
        return RedirectResponse("/users?err=No+such+user", status_code=303)
    if _private(user, admin):
        # Opted out of sharing: the link goes to the user's own inbox, never to
        # the admin (otherwise an admin could reset and sign in as them).
        if not (email_configured() and user.email):
            return RedirectResponse(
                "/users?err=" + quote_plus(f"Can't reach {user.username} by email — "
                                           "check the email settings."), status_code=303)
        token = authn.create_reset_token(session, user)
        send_email(user.email, "Reset your Run·dere password",
                   "An administrator sent you a password-reset link (valid ~1 hour):\n\n"
                   f"{_base_url(request)}/reset/{token}\n\n"
                   "If you didn't ask for this, you can ignore it.")
        _audit(session, user, admin, "reset_link_emailed")
        session.commit()
        return RedirectResponse(f"/users?msg=Reset+link+emailed+to+{user.username}",
                                status_code=303)
    token = authn.create_reset_token(session, user)
    _audit(session, user, admin, "reset_link_shown")
    session.commit()
    link = f"{_base_url(request)}/reset/{token}"
    return RedirectResponse(f"/users?link={link}&link_for={user.username}", status_code=303)


@router.post("/users/{user_id}/admin")
def users_toggle_admin(user_id: str, admin: User = Depends(authn.require_admin),
                       session: Session = Depends(get_session)):
    user = _u(session, user_id)
    if user is None:
        return RedirectResponse("/users?err=No+such+user", status_code=303)
    if user.id == admin.id:
        return RedirectResponse("/users?err=You+can%27t+change+your+own+admin+role", status_code=303)
    if user.is_admin and session.query(User).filter_by(is_admin=True).count() <= 1:
        return RedirectResponse("/users?err=Can%27t+remove+the+last+admin", status_code=303)
    user.is_admin = not user.is_admin
    _audit(session, user, admin, "admin_on" if user.is_admin else "admin_off")
    session.commit()
    _notify_private(user, "granted you admin" if user.is_admin else "removed your admin role")
    return RedirectResponse(f"/users?msg=Updated+{user.username}", status_code=303)


@router.post("/users/{user_id}/active")
def users_toggle_active(user_id: str, admin: User = Depends(authn.require_admin),
                        session: Session = Depends(get_session)):
    user = _u(session, user_id)
    if user is None:
        return RedirectResponse("/users?err=No+such+user", status_code=303)
    if user.id == admin.id:
        return RedirectResponse("/users?err=You+can%27t+deactivate+yourself", status_code=303)
    user.is_active = not user.is_active
    user.token_version += 1                    # kick out any live sessions on deactivate
    _audit(session, user, admin, "activated" if user.is_active else "deactivated")
    session.commit()
    _notify_private(user, "reactivated your account" if user.is_active
                    else "deactivated your account")
    return RedirectResponse(f"/users?msg=Updated+{user.username}", status_code=303)


@router.post("/users/{user_id}/traveler")
def users_link_traveler(user_id: str, traveler_id: str = Form("none"),
                        new_traveler: str = Form(""),
                        admin: User = Depends(authn.require_admin),
                        session: Session = Depends(get_session)):
    user = _u(session, user_id)
    if user is None:
        return RedirectResponse("/users?err=No+such+user", status_code=303)
    user.traveler_id = authn.resolve_traveler(session, traveler_id=traveler_id,
                                              new_name=new_traveler)
    _audit(session, user, admin, "traveller_linked", str(user.traveler_id or ""))
    session.commit()
    _notify_private(user, "changed which traveller your account is linked to")
    return RedirectResponse(f"/users?msg=Linked+traveller+for+{user.username}", status_code=303)
