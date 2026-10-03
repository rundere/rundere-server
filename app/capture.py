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
from extract import apple_maps_url, maps_url, place_url

log = logging.getLogger("yonder.capture")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))
from app.icons import install as _install_icons  # noqa: E402
_install_icons(templates)

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
        req = urllib.request.Request(url, headers={"User-Agent": "Rundere-web-capture"})
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
               notes: str | None = None, collection_id: str | None = None,
               owner_user_id: str | None = None, trip_id: str | None = None,
               via: str | None = None) -> str:
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
            "trip_id": trip_id or None,
            # "bookmarklet" when the page came through the bookmarklet hand-off, so the
            # setup guide's per-tab "last capture" doesn't count capture-box pastes.
            "via": via or None,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    batch = store_batch(session, body, payload, _internal_key_id(session),
                        user_id=owner_user_id)
    session.commit()
    return batch.id


def _queue_maps(session: Session, url: str, *, submitted_by: str | None,
                travel_date: str, travel_time: str | None = None,
                end_date: str | None = None, timezone: str | None = None,
                mode: str | None = None,
                title: str | None = None, notes: str | None = None,
                collection_id: str | None = None,
                owner_user_id: str | None = None,
                provider: str = "google", trip_id: str | None = None) -> str:
    """Store a maps directions link (Google or Apple) as a directions-channel
    batch — the native replacement for the old n8n form. The extractor needs a
    travel date (a route carries none, RULE-31); the caller validates it.
    `provider` selects the channel/extractor; `mode` is the car/transit/walk
    selection cross-checked against the URL (SPEC-4 §2/§3)."""
    channel = "apple_maps_url" if provider == "apple" else "maps_url"
    payload = {
        "capture_channel": channel,
        "url": url,
        "capture_context": {
            "submitted_by": submitted_by or _WEB_CLIENT,
            "travel_date": travel_date,
            "travel_time": travel_time or None,
            "end_date": end_date or None,
            "timezone": timezone or None,
            "mode": mode or None,
            "title": title or None,
            "notes": notes or None,
            "collection_id": collection_id or None,
            "trip_id": trip_id or None,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    batch = store_batch(session, body, payload, _internal_key_id(session),
                        user_id=owner_user_id)
    session.commit()
    return batch.id


def _filing_trips(session: Session, user) -> list:
    """Trips this person can file a capture into, for the capture form's picker:
    upcoming and current first (soonest first), then past ones (latest first)."""
    from datetime import date

    from app import scope
    from app.models import Trip
    trips = session.query(Trip).filter(scope.filing_trip_filter(session, user)).all()
    today = date.today().isoformat()
    ahead = sorted((t for t in trips if (t.end_date or "") >= today),
                   key=lambda t: t.start_date or "")
    past = sorted((t for t in trips if (t.end_date or "") < today),
                  key=lambda t: t.start_date or "", reverse=True)
    return ahead + past


def _origin(request: Request) -> str:
    """scheme://host[:port] this request came in on: where the bookmarklet's tab opens."""
    u = request.base_url
    return f"{u.scheme}://{u.netloc}"


def _bookmarklet(origin: str) -> str:
    """The bookmarklet source (SPEC-9 D-159). It hands the page to Run·dere by opening
    `/capture/handoff` and posting the HTML to that tab with `postMessage`: the Run·dere tab is
    same-origin (cookie attached, no CORS) and unaffected by the booking site's CSP, which is why
    the old cross-origin `fetch` never worked. If pop-ups are blocked it copies the page HTML to
    the clipboard so it can be pasted into /capture."""
    return (
        "javascript:(function(){"
        "var O='%s',h=document.documentElement.outerHTML,u=location.href;"
        "var cp=function(){var t=document.createElement('textarea');t.value=h;"
        "t.style.position='fixed';t.style.top='-9999px';document.body.appendChild(t);"
        "t.focus();t.select();var k=false;try{k=document.execCommand('copy')}catch(e){}"
        "document.body.removeChild(t);return k};"
        "var fb=function(){var say=function(k){alert(k?"
        "'Run·dere could not open its tab (pop-ups may be blocked). The page HTML is now on your "
        "clipboard. Open Run·dere Capture and paste it into the box.':"
        "'Run·dere could not open its tab or copy this page. Open Run·dere Capture and paste the "
        "page source manually.')};"
        "if(navigator.clipboard&&navigator.clipboard.writeText){"
        "navigator.clipboard.writeText(h).then(function(){say(true)},function(){say(cp())})}"
        "else{say(cp())}};"
        "var w=window.open(O+'/capture/handoff','rundere_capture');"
        "if(!w){fb();return}"
        "var done=false;"
        "function on(e){if(e.origin!==O||!e.data||e.data.type!=='rundere-ready')return;"
        "w.postMessage({type:'rundere-page',html:h,url:u},O);done=true;"
        "window.removeEventListener('message',on)}"
        "window.addEventListener('message',on);"
        "setTimeout(function(){if(!done)window.removeEventListener('message',on)},300000)"
        "})()" % origin
    )


# --------------------------------------------------------------- after a capture (D-158)
_POLL_MS = 1500
_POLL_MAX = 80                       # ~2 minutes, then "still working — it will appear"
_BUCKET_LABELS = {"attention": "Needs you", "waiting": "Waiting for a trip",
                  "ready": "Ready to approve", "staged": "Approved", "logged": "Logged",
                  "ignored": "Ignored"}


def _captured(request: Request, batch_id: str, user):
    """The capture succeeded: start processing (D-158) and return the self-polling result
    panel. Processing is best-effort, so a failure here never fails the capture."""
    from app.review import autoprocess
    try:
        autoprocess.kick(user)
    except Exception:
        log.exception("auto-process could not start; the batch stays received")
    return templates.TemplateResponse(request, "_capture_progress.html", {
        "batch_id": batch_id, "n": 0, "poll_ms": _POLL_MS,
        "enabled": settings.auto_process})


@router.get("/capture/batch/{batch_id}", response_class=HTMLResponse)
def capture_batch(batch_id: str, request: Request, n: int = 0,
                  session: Session = Depends(get_session)):
    """Poll target for the result panel: still working, or what landed in the queue. Only the
    capturing person's own batch is visible (same scope as the review queue)."""
    from app import authn, scope
    from app.models import IngestBatch, TravelItem
    user = authn.resolve_user(session, request)
    if user is None and settings.auth_required:
        return HTMLResponse("", status_code=404)
    q = session.query(IngestBatch).filter(IngestBatch.id == batch_id)
    if not scope.unrestricted(user):
        q = q.filter(scope.batch_filter(user))
    batch = q.first()
    if batch is None:
        return HTMLResponse("", status_code=404)
    items = (session.query(TravelItem).filter(TravelItem.batch_id == batch.id)
             .order_by(TravelItem.segment_index).all())
    settled = batch.status != "received" and all(
        (i.status or {}).get("resolve") != "pending" for i in items)
    if not settled:
        if not settings.auto_process or n >= _POLL_MAX:
            return templates.TemplateResponse(request, "_capture_cards.html", {
                "cards": [], "state": "waiting"})
        return templates.TemplateResponse(request, "_capture_progress.html", {
            "batch_id": batch_id, "n": n + 1, "poll_ms": _POLL_MS, "enabled": True})
    from app.review.routes import _view
    cards = []
    for it in items:
        v = _view(it)
        cards.append({"id": v["id"], "kind": v["kind"],
                      "headline": v["headline"], "subline": v["subline"],
                      "note": v["note"], "bucket": v["bucket"],
                      "bucket_label": _BUCKET_LABELS.get(v["bucket"], v["bucket"])})
    return templates.TemplateResponse(request, "_capture_cards.html", {
        "cards": cards, "state": "done" if cards else "empty"})


@router.get("/capture", response_class=HTMLResponse)
def capture_page(request: Request, session: Session = Depends(get_session)):
    from app import authn
    user = authn.resolve_user(session, request)
    trips = _filing_trips(session, user)
    return templates.TemplateResponse(request, "capture.html", {
        "trips": trips, "recent": _recent_captures(session, user)})


def _own_batches(session: Session, user, limit: int):
    """The person's own recent batches (same scope as the review queue), newest first."""
    from app import scope
    from app.models import IngestBatch
    q = session.query(IngestBatch)
    if not scope.unrestricted(user):
        q = q.filter(scope.batch_filter(user))
    elif settings.auth_required:
        return []
    return q.order_by(IngestBatch.received_at.desc()).limit(limit).all()


def _recent_captures(session: Session, user, limit: int = 6) -> list[dict]:
    """Recent captures with what became of them, so a capture is never 'lost between pages'."""
    from app.models import TravelItem
    batches = _own_batches(session, user, limit)
    if not batches:
        return []
    from app.review.routes import _view
    by_batch: dict[str, list] = {}
    for it in session.query(TravelItem).filter(
            TravelItem.batch_id.in_([b.id for b in batches])).all():
        by_batch.setdefault(it.batch_id, []).append(it)
    out = []
    for b in batches:
        items = by_batch.get(b.id, [])
        v = _view(items[0]) if items else None
        out.append({
            "when": b.received_at, "channel": (b.payload or {}).get("capture_channel") or "gmail",
            "headline": v["headline"] if v else None,
            "count": len(items),
            "state": _BUCKET_LABELS.get(v["bucket"], v["bucket"]) if v else (
                "Waiting to be read" if b.status == "received" else "Nothing read"),
            "bucket": v["bucket"] if v else None,
        })
    return out


# --------------------------------------------------------------- "Other ways to capture" (D-160)
_CHANNELS = [  # (tab key, label, capture_channel values it produces)
    ("gmail", "Gmail", ("gmail",)),
    ("calendar", "Calendar", ("gcal",)),
    ("bookmarklet", "Bookmarklet", ("bookmarklet",)),
    ("email", "Email forward", ()),
    ("ios", "iPhone and iPad", ()),
    ("mcp", "AI assistants (MCP)", ()),
]
_CAPTURE_DIR = Path(__file__).resolve().parent.parent / "capture"


def _addon_file(addon: str, name: str) -> str | None:
    """Text of an add-on file the guide asks the person to copy, or None when this install
    does not carry it (the guide then links to the repository file instead)."""
    f = _CAPTURE_DIR / addon / name
    try:
        return f.read_text(encoding="utf-8")
    except OSError:
        return None


def _last_capture_by_channel(session: Session, user) -> dict[str, object]:
    """Most recent capture time per capture_channel, for the status line on each tab."""
    seen: dict[str, object] = {}
    for b in _own_batches(session, user, 200):
        ch = (b.payload or {}).get("capture_channel") or "gmail"
        if ch == "web" and ((b.payload or {}).get("capture_context") or {}).get("via") == "bookmarklet":
            ch = "bookmarklet"          # the capture box also uses the web channel
        seen.setdefault(ch, b.received_at)
    return seen


def _ago(dt) -> str:
    from datetime import datetime, timezone
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 90:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= size:
            n = secs // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


@router.get("/capture/handoff", response_class=HTMLResponse)
def capture_handoff(request: Request, session: Session = Depends(get_session)):
    """The tab the bookmarklet opens (D-159). It tells the opener it is ready, receives the page
    HTML by postMessage, and submits it through the normal capture path, so the result cards of
    D-158 appear here. Signing in first when sign-in is required keeps the capture the person's."""
    from fastapi.responses import RedirectResponse

    from app import authn
    if authn.resolve_user(session, request) is None and settings.auth_required:
        return RedirectResponse("/login?next=/capture/handoff", status_code=303)
    return templates.TemplateResponse(request, "capture_handoff.html", {
        "max_bytes": settings.ingest_max_body_bytes})


@router.get("/capture/setup", response_class=HTMLResponse)
def capture_setup(request: Request, tab: str = "gmail", session: Session = Depends(get_session)):
    """The click-through guide: one tab per channel that is set up once, each an instruction
    manual (SPEC-9 D-160). The always-used capture box stays on /capture."""
    from app import authn
    user = authn.resolve_user(session, request)
    keys = [k for k, _, _ in _CHANNELS]
    tab = "mcp" if tab == "claude" else tab            # the tab was called Claude before it was generic
    tab = tab if tab in keys else "gmail"
    last = _last_capture_by_channel(session, user)
    status = {}
    for key, _, chans in _CHANNELS:
        times = [last[c] for c in chans if c in last]
        status[key] = _ago(max(times)) if times else ""
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    return templates.TemplateResponse(request, "capture_setup.html", {
        "tab": tab, "tabs": [{"key": k, "label": lbl, "on": k == tab, "status": status[k]}
                              for k, lbl, _ in _CHANNELS],
        "ingest_url": base + "/ingest", "ingest_is_public": bool(settings.public_base_url),
        "mcp_url": base + "/mcp",
        "bookmarklet": _bookmarklet(_origin(request)),
        "gmail_code": _addon_file("gmail-addon", "Code.gs"),
        "gmail_manifest": _addon_file("gmail-addon", "appsscript.json"),
        "gcal_code": _addon_file("gcal-addon", "Code.gs"),
        "gcal_manifest": _addon_file("gcal-addon", "appsscript.json"),
        "signed_in": user is not None or not settings.auth_required,
    })


@router.post("/capture/setup/key", response_class=HTMLResponse)
def capture_setup_key(request: Request, channel: str = Form("gmail"),
                      session: Session = Depends(get_session)):
    """Make the person's own ingest key for an add-on (YONDER-161), shown once. It is scoped to
    them (D-120), so what the add-on sends is theirs. Making another replaces the old one."""
    from datetime import datetime, timezone

    from app import auth, authn
    from app.models import ApiKey
    user = authn.resolve_user(session, request)
    if user is None and settings.auth_required:
        return HTMLResponse('<div class="cap-msg err">Sign in to make a key.</div>', status_code=401)
    channel = "calendar" if channel == "calendar" else "gmail"
    name = f"{channel}-addon-{user.username}" if user is not None else f"{channel}-addon"
    key = auth.generate_key()
    row = session.query(ApiKey).filter_by(client_name=name).one_or_none()
    if row is None:
        session.add(ApiKey(client_name=name, key_hash=auth.hash_key(key),
                           user_id=user.id if user is not None else None))
    else:                                    # rotation: the old key stops working
        row.key_hash = auth.hash_key(key)
        row.created_at = datetime.now(timezone.utc)
        row.revoked_at = None
        row.last_used_at = None
        row.user_id = user.id if user is not None else None
    session.commit()
    resp = templates.TemplateResponse(request, "_capture_key.html", {
        "key": key, "channel": channel,
        "ingest_url": (settings.public_base_url or str(request.base_url)).rstrip("/") + "/ingest"})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/capture/web")
async def capture_web(request: Request, session: Session = Depends(get_session)):
    """Two front doors:
      - the BOOKMARKLET posts raw HTML (Content-Type: text/html) → plain-text
        reply (the bookmarklet alerts it);
      - the PAGE FORM posts fields → an HTMX partial rendered inline.
    """
    ctype = request.headers.get("Content-Type", "")
    submitted_by = _submitted_by(request)
    # Phase 2 (D-120): attribute the capture to the submitting Yonder user (from
    # the session cookie / bearer / trusted proxy). None => an open capture.
    from app import authn
    _owner = authn.resolve_user(session, request)
    owner_user_id = _owner.id if _owner else None

    # --- bookmarklet path: raw page HTML in the body ---
    if ctype.startswith("text/html"):
        raw = (await request.body()).decode("utf-8", errors="replace")
        if len(raw) > settings.ingest_max_body_bytes:
            return PlainTextResponse("Run·dere: page too large to capture.", status_code=413)
        if not raw.strip():
            return PlainTextResponse("Run·dere: empty page — nothing captured.", status_code=400)
        source_url = request.headers.get("X-Yonder-Source-Url") or request.headers.get("Referer")
        batch_id = _queue_web(session, raw, source_url=source_url, submitted_by=submitted_by, owner_user_id=owner_user_id,
                                via="bookmarklet")      # the legacy header-based bookmarklet
        return PlainTextResponse(
            f"Run·dere captured this page ✓ (batch {batch_id[:8]}). "
            f"Run “Process new captures” in the review queue to extract it.")

    # --- page form path ---
    form = await request.form()
    content = (form.get("content") or "").strip()
    path = (form.get("path") or "").strip().lower()   # page | google | apple | ""
    travel_date = (form.get("travel_date") or "").strip() or None
    travel_time = (form.get("travel_time") or "").strip() or None
    end_date = (form.get("end_date") or "").strip() or None
    timezone = (form.get("timezone") or "").strip() or None
    mode = (form.get("mode") or "").strip() or None
    title = (form.get("title") or "").strip() or None
    notes = (form.get("notes") or "").strip() or None
    collection_id = (form.get("collection_id") or "").strip() or None
    trip_id = (form.get("trip_id") or "").strip() or None

    def _msg(ok, message, batch_id=None):
        return templates.TemplateResponse(request, "_capture_result.html", {
            "ok": ok, "message": message, "batch_id": batch_id})

    if not content:
        return _msg(False, "Paste a page's source or a URL first.")

    def _queue_directions(provider):
        if not travel_date:
            return _msg(False, "That’s a directions capture — add a Travel date "
                               "(a route has no date of its own).")
        batch_id = _queue_maps(session, content, submitted_by=submitted_by,
                               travel_date=travel_date, travel_time=travel_time,
                               end_date=end_date, timezone=timezone, mode=mode,
                               title=title, notes=notes, collection_id=collection_id,
                               trip_id=trip_id,
                               owner_user_id=owner_user_id, provider=provider)
        return _captured(request, batch_id, _owner)

    def _save_place():
        """A Maps PLACE link (YONDER-200): kept as a saved place, not sent down the
        bookings pipeline (D-167). Nothing here needs a date or a trip."""
        from app import places as places_svc
        if _owner is None and settings.auth_required:
            # /capture is a public path, so the route decides: with sign-in on, an
            # anonymous paste must not write an ownerless row (YONDER-186) or make
            # the server fetch and look things up for a stranger.
            return _msg(False, "Sign in to save places.")
        try:
            place, outcome = places_svc.save(session, _owner, url=content,
                                             source="web", enrich=True, revive=True)
        except ValueError as exc:
            return _msg(False, str(exc))
        said = {"created": f"Saved “{place.name}”.",
                "revived": f"Saved “{place.name}” again (you had removed it).",
                "duplicate": f"“{place.name}” was already in your saved places.",
                "near_duplicate": f"“{place.name}” looks like a place you already have, so it was "
                                  "not added again.",
                "already_filed": f"“{place.name}” was already filed to a trip.",
                "removed_earlier": f"You removed “{place.name}” earlier."}[outcome]
        return templates.TemplateResponse(request, "_capture_result.html", {
            "ok": True, "message": said,
            "place_id": place.id if outcome in ("created", "revived", "duplicate",
                                                "near_duplicate") else None})

    # Explicit directions paths: the selection is authoritative, so looks_like_*
    # validates the pasted URL against the chosen provider (SPEC-4 §2.1) — but a
    # place link is no directions link, so say so instead of failing downstream.
    if path in ("apple", "google") and place_url.classify(content, expand=False) == "place":
        return _msg(False, "That’s a place link, not directions. Set the type to "
                           "automatic to save it as a place.")
    if path == "apple":
        if apple_maps_url.is_apple_short_link(content):
            return _msg(False, "That Apple Maps share link (maps.apple/p/…) carries "
                               "no coordinates — open it and paste the full "
                               "maps.apple.com directions link instead.")
        if not apple_maps_url.looks_like_apple_maps_url(content):
            return _msg(False, "That doesn’t look like an Apple Maps directions link "
                               "(needs maps.apple.com with saddr and daddr).")
        return _queue_directions("apple")
    if path == "google":
        if not maps_url.looks_like_maps_url(content):
            return _msg(False, "That doesn’t look like a Google Maps directions link.")
        return _queue_directions("google")

    source_url = None
    html = content
    handed_url = (form.get("source_url") or "").strip()      # the bookmarklet hand-off (D-159)
    if handed_url and _looks_like_url(handed_url) and not _looks_like_url(content):
        source_url = handed_url
    if _looks_like_url(content):
        # No explicit provider path (bookmarklet / older clients): fall back to
        # sniffing so a pasted maps link still routes correctly. An explicit
        # "page" selection skips this and is fetched as a web page.
        if path != "page":
            if place_url.classify(content) == "place":      # expands a Google short link
                return _save_place()
            is_apple = apple_maps_url.looks_like_apple_maps_url(content)
            if apple_maps_url.is_apple_short_link(content):
                return _msg(False, "That Apple Maps share link (maps.apple/p/…) "
                                   "carries no coordinates — paste the full "
                                   "maps.apple.com directions link instead.")
            if is_apple or maps_url.looks_like_maps_url(content):
                return _queue_directions("apple" if is_apple else "google")
        source_url = content
        html = _fetch_public_html(content)
        if not html:
            return _msg(False, "Couldn’t fetch that URL — it may need a login, block "
                               "bots, or resolve to a private address. Use the "
                               "bookmarklet on the open page, or paste its source "
                               "here instead.")

    if len(html.encode("utf-8")) > settings.ingest_max_body_bytes:
        return templates.TemplateResponse(request, "_capture_result.html", {
            "ok": False, "message": "That page is too large to capture."})

    batch_id = _queue_web(session, html, source_url=source_url, submitted_by=submitted_by,
                          travel_date=travel_date, travel_time=travel_time, title=title,
                          notes=notes, collection_id=collection_id, owner_user_id=owner_user_id,
                          trip_id=trip_id,
                          via=("bookmarklet" if form.get("via") == "bookmarklet" else None))
    return _captured(request, batch_id, _owner)


@router.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse(request, "about.html", {
        "version": settings.app_version,
        "repo_url": "https://github.com/jeeves5454/rundere-server",
    })
