"""Account › Export my data (SPEC-13 §2.5, D-241).

- **/account/export**            what the archive holds, the build button, where a
                                 build stands, when the file expires, the download.
- **/account/export/build**      start a build (POST).
- **/account/export/status**     the status panel alone, polled while a build runs.
- **/account/export/{id}/download**  the zip.

Web only: every route takes `confirm.web_user`, which refuses bearer
credentials, so there is no way to an export through /api/v1, MCP, a personal
API token or an OAuth token, and no admin route to anyone else's. The download
also needs a fresh confirmation of who the person is (app/confirm.py), and
someone else's export id answers 404. With sign-in off the pages don't exist;
on a demo server `demo_guard` answers 404 before any of this runs (D-198).
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import archive, archive_jobs, confirm
from app.config import settings
from app.db import get_session
from app.email import email_configured
from app.models import AccountEvent, User

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))
from app.icons import install as _install_icons  # noqa: E402
_install_icons(templates)


def _ctx(request: Request, session: Session, user: User, **extra) -> dict:
    row = archive_jobs.latest(session, user.id)
    return {
        "u": user, "export": row,
        "building": row is not None and row.status == archive_jobs.BUILDING,
        "ready": archive_jobs.downloadable(row),
        "expires_at": archive_jobs._aware(row.expires_at) if row is not None else None,
        "size": _size(row.size_bytes) if row is not None and row.size_bytes else "",
        "confirmed": confirm.is_fresh(request, user),
        "keep_days": archive_jobs.KEEP_DAYS, "max_per_day": archive_jobs.MAX_PER_DAY,
        "will_email": bool(user.email) and email_configured(),
        "not_included": archive.NOT_INCLUDED, "msg": "", "err": "", **extra}


def _size(n: int) -> str:
    from app.documents import _mb
    return _mb(n)


@router.get("/account/export", response_class=HTMLResponse)
def export_page(request: Request, msg: str = "", err: str = "",
                user: User = Depends(confirm.web_user),
                session: Session = Depends(get_session)):
    resp = templates.TemplateResponse(request, "account_export.html",
                                      _ctx(request, session, user, msg=msg, err=err))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/account/export/status", response_class=HTMLResponse)
def export_status(request: Request, user: User = Depends(confirm.web_user),
                  session: Session = Depends(get_session)):
    resp = templates.TemplateResponse(request, "_export_status.html",
                                      _ctx(request, session, user))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/account/export/build")
def export_build(request: Request, user: User = Depends(confirm.web_user),
                 session: Session = Depends(get_session)):
    person = session.get(User, user.id)
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    try:
        archive_jobs.launch(session, person, base_url=base)
    except archive_jobs.Refused as why:
        text = str(why)
        return RedirectResponse(
            f"/account/export?err={quote_plus(text[:1].upper() + text[1:] + '.')}",
            status_code=303)
    return RedirectResponse("/account/export", status_code=303)


@router.get("/account/export/{export_id}/download")
def export_download(request: Request, export_id: str,
                    user: User = Depends(confirm.web_user),
                    session: Session = Depends(get_session)):
    row = archive_jobs.own(session, user, export_id)
    if row is None or not archive_jobs.downloadable(row):
        # Someone else's id, an expired or replaced one, a missing one: all the same.
        raise HTTPException(status_code=404, detail="not found")
    if not confirm.is_fresh(request, user):
        # Back to the export page afterwards, not to this URL: the download
        # then starts on a click, never as the tail of a redirect chain.
        return RedirectResponse(confirm.page_url("/account/export"), status_code=303)
    session.add(AccountEvent(user_id=user.id, actor_id=user.id, action="export_downloaded"))
    session.commit()
    finished = archive_jobs._aware(row.finished_at)
    name = archive.archive_name(user, finished.date() if finished else None)
    return FileResponse(
        archive_jobs.path_for(row), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"',
                 "X-Content-Type-Options": "nosniff",
                 "Cache-Control": "no-store"})
