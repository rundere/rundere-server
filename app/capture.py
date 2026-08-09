"""Web capture surface (1.1) — the native replacement for a Maps-style n8n form.

`GET /capture`      the page: a bookmarklet + a paste/URL form.
`POST /capture/web` stores a `web`-channel ingest batch.

This route lives on the PRIVATE app only (the same surface as /review and
/admin), so it is authenticated by whatever reverse proxy fronts that host —
NOT by an API key + HMAC like /ingest. There is deliberately no signature
check here: the proxy already proved who the caller is. If the proxy forwards
an identity header we record it as `submitted_by`; otherwise the capture is
still accepted (the auth decision was the proxy's to make, not ours).

`ingest_batches.api_key_id` is a non-null FK, but a proxy-authed capture has no
client key — so batches are attributed to a single inert internal key row that
is never a usable /ingest credential (its hash matches no issued key).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import secrets
import socket
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.ingest import store_batch
from app.models import ApiKey
from extract import maps_url

log = logging.getLogger("travel-extractor.capture")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))

_WEB_CLIENT = "web-capture"
# Proxy identity headers, in the order we trust them. Best-effort: the feature
# never depends on any one auth product being present.
_IDENTITY_HEADERS = ("X-authentik-username", "X-Forwarded-User",
                     "X-Auth-Request-User", "Remote-User")


def _submitted_by(request: Request) -> str | None:
    for h in _IDENTITY_HEADERS:
        v = request.headers.get(h)
        if v:
            return v
    return None


def _internal_key_id(session: Session) -> str:
    """The api_key_id used to attribute proxy-authed captures. Get-or-create a
    single inert row — its key_hash is random, so it can never be presented as a
    working /ingest key; it exists only to satisfy the non-null FK and to make
    web captures legible in the ingest logs."""
    row = session.query(ApiKey).filter_by(client_name=_WEB_CLIENT).one_or_none()
    if row is None:
        row = ApiKey(client_name=_WEB_CLIENT,
                     key_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest())
        session.add(row)
        session.flush()          # need row.id before store_batch references it
    return row.id


def _looks_like_url(text: str) -> bool:
    t = (text or "").strip()
    return t.lower().startswith(("http://", "https://")) and "<" not in t and "\n" not in t


def _fetch_public_html(url: str) -> str | None:
    """Fetch a PUBLIC page's HTML, or None. SSRF-guarded: only http(s), and the
    hostname must not resolve to a private/loopback/link-local address — this
    route is operator-only, but it must still never be a pivot into the LAN.
    Best-effort: any failure returns None and the caller tells the operator to
    use the bookmarklet or paste the source instead."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError:
        return None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return None
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Yonder-web-capture"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "xml" not in ctype:
                return None
            raw = resp.read(settings.ingest_max_body_bytes)
            charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except Exception:
        return None


def _queue_web(session: Session, html: str, *, source_url: str | None,
               submitted_by: str | None, travel_date: str | None = None,
               travel_time: str | None = None, title: str | None = None,
               notes: str | None = None, collection_id: str | None = None) -> str:
    """Wrap a captured page into a `web`-channel batch and store it. Returns the
    batch id."""
    payload = {
        "capture_channel": "web",
        "html": html,
        "source_url": source_url or None,
        "capture_context": {
            "submitted_by": submitted_by or _WEB_CLIENT,
            "travel_date": travel_date or None,
            "travel_time": travel_time or None,
            "title": title or None,
            "notes": notes or None,
            "collection_id": collection_id or None,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    batch = store_batch(session, body, payload, _internal_key_id(session))
    session.commit()
    return batch.id


def _queue_maps(session: Session, url: str, *, submitted_by: str | None,
                travel_date: str, travel_time: str | None = None,
                title: str | None = None, notes: str | None = None,
                collection_id: str | None = None) -> str:
    """Store a Google Maps directions link as a `maps_url`-channel batch — the
    native replacement for the old n8n form. The maps_url extractor needs a
    travel date (a route carries none, RULE-31); the caller validates it."""
    payload = {
        "capture_channel": "maps_url",
        "url": url,
        "capture_context": {
            "submitted_by": submitted_by or _WEB_CLIENT,
            "travel_date": travel_date,
            "travel_time": travel_time or None,
            "title": title or None,
            "notes": notes or None,
            "collection_id": collection_id or None,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    batch = store_batch(session, body, payload, _internal_key_id(session))
    session.commit()
    return batch.id


@router.get("/capture", response_class=HTMLResponse)
def capture_page(request: Request):
    # The bookmarklet POSTs the current tab's rendered HTML back to THIS host —
    # request.base_url is the private host the operator loaded /capture on, which
    # is exactly where /capture/web lives.
    endpoint = str(request.base_url).rstrip("/") + "/capture/web"
    # If the direct POST is blocked (a strict Content-Security-Policy on the
    # page can forbid a bookmarklet's fetch to another origin) or otherwise
    # fails, fall back to copying the page HTML to the clipboard so the operator
    # can paste it into /capture in one step — the reliable escape hatch for
    # heavy/locked-down sites.
    bookmarklet = (
        "javascript:(function(){"
        "var h=document.documentElement.outerHTML;"
        "var cp=function(){var t=document.createElement('textarea');t.value=h;"
        "t.style.position='fixed';t.style.top='-9999px';document.body.appendChild(t);"
        "t.focus();t.select();var k=false;try{k=document.execCommand('copy')}catch(e){}"
        "document.body.removeChild(t);return k};"
        "var fb=function(){var say=function(k){alert(k?"
        "'Yonder could not post to the server (this page may block it). The page "
        "HTML is now on your clipboard. Open Yonder /capture and paste it into the box.':"
        "'Yonder could not post or auto-copy on this page. Open Yonder /capture and "
        "paste the page source manually.')};"
        "if(navigator.clipboard&&navigator.clipboard.writeText){"
        "navigator.clipboard.writeText(h).then(function(){say(true)},function(){say(cp())})}"
        "else{say(cp())}};"
        "fetch('%s',{method:'POST',credentials:'include',"
        "headers:{'Content-Type':'text/html'},body:h})"
        ".then(function(r){return r.text()}).then(function(t){alert(t)})"
        ".catch(function(){fb()})})()" % endpoint
    )
    return templates.TemplateResponse(request, "capture.html", {
        "bookmarklet": bookmarklet, "endpoint": endpoint})


@router.post("/capture/web")
async def capture_web(request: Request, session: Session = Depends(get_session)):
    """Two front doors:
      - the BOOKMARKLET posts raw HTML (Content-Type: text/html) → plain-text
        reply (the bookmarklet alerts it);
      - the PAGE FORM posts fields → an HTMX partial rendered inline.
    """
    ctype = request.headers.get("Content-Type", "")
    submitted_by = _submitted_by(request)

    # --- bookmarklet path: raw page HTML in the body ---
    if ctype.startswith("text/html"):
        raw = (await request.body()).decode("utf-8", errors="replace")
        if len(raw) > settings.ingest_max_body_bytes:
            return PlainTextResponse("Yonder: page too large to capture.", status_code=413)
        if not raw.strip():
            return PlainTextResponse("Yonder: empty page — nothing captured.", status_code=400)
        source_url = request.headers.get("X-Yonder-Source-Url") or request.headers.get("Referer")
        batch_id = _queue_web(session, raw, source_url=source_url, submitted_by=submitted_by)
        return PlainTextResponse(
            f"Yonder captured this page ✓ (batch {batch_id[:8]}). "
            f"Run “Process new captures” in the review queue to extract it.")

    # --- page form path ---
    form = await request.form()
    content = (form.get("content") or "").strip()
    travel_date = (form.get("travel_date") or "").strip() or None
    travel_time = (form.get("travel_time") or "").strip() or None
    title = (form.get("title") or "").strip() or None
    notes = (form.get("notes") or "").strip() or None
    collection_id = (form.get("collection_id") or "").strip() or None

    if not content:
        return templates.TemplateResponse(request, "_capture_result.html", {
            "ok": False, "message": "Paste a page's source or a URL first."})

    source_url = None
    html = content
    if _looks_like_url(content):
        # A Google Maps directions link is not a web page to fetch — route it to
        # the maps_url extractor (native; retires the n8n form). It needs a date.
        if maps_url.looks_like_maps_url(content):
            if not travel_date:
                return templates.TemplateResponse(request, "_capture_result.html", {
                    "ok": False,
                    "message": ("That’s a Google Maps directions link — add a "
                                "Travel date (a route has no date of its own).")})
            batch_id = _queue_maps(session, content, submitted_by=submitted_by,
                                   travel_date=travel_date, travel_time=travel_time,
                                   title=title, notes=notes, collection_id=collection_id)
            return templates.TemplateResponse(request, "_capture_result.html", {
                "ok": True, "batch_id": batch_id,
                "message": "Captured the drive. Run “Process new captures” in the review queue to extract it."})
        source_url = content
        html = _fetch_public_html(content)
        if not html:
            return templates.TemplateResponse(request, "_capture_result.html", {
                "ok": False,
                "message": ("Couldn’t fetch that URL — it may need a login, block "
                            "bots, or resolve to a private address. Use the bookmarklet "
                            "on the open page, or paste its source here instead.")})

    if len(html.encode("utf-8")) > settings.ingest_max_body_bytes:
        return templates.TemplateResponse(request, "_capture_result.html", {
            "ok": False, "message": "That page is too large to capture."})

    batch_id = _queue_web(session, html, source_url=source_url, submitted_by=submitted_by,
                          travel_date=travel_date, travel_time=travel_time, title=title,
                          notes=notes, collection_id=collection_id)
    return templates.TemplateResponse(request, "_capture_result.html", {
        "ok": True, "batch_id": batch_id,
        "message": "Captured. Run “Process new captures” in the review queue to extract it."})


@router.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse(request, "about.html", {
        "version": settings.app_version,
        "repo_url": "https://github.com/jeeves5454/yonder",
    })
