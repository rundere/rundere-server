"""Home Profiles editor — purpose-built (D-07/D-39).

SQLAdmin's generic forms cannot do the interactive parts the owner asked for:
verify an airport code against AirTrail and pull its coordinates, or geocode a
residence address via Photon. So home profiles are edited here, in the HTMX
app, and dropped from SQLAdmin.

Field rules follow the kind:
  home_airport         -> IATA code required; coords come from AirTrail (or
                          Photon fallback), never typed by hand.
  residence/home_region-> latitude+longitude required; filled from a Photon
                          address lookup, editable afterwards.

Per person (4.1): everyone sees and edits only their OWN homes — the ones on
their linked traveller — admins included (D-124: an admin administers the
platform, not other people's personal data). With auth off it's the deployment
owner's (`self`) homes, as before.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import scope
from app.config import settings
from app.db import get_session
from app.models import HomeProfile
from app.resolving import OWNER_HOMES, home_rows_query, self_traveler_id

log = logging.getLogger("yonder.home")
router = APIRouter(prefix="/review/home")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

KINDS = [
    ("home_airport", "Home airport — an airport you fly from"),
    ("residence", "Residence — a place you live"),
    ("home_region", "Home region — a broader area"),
]


def _airtrail():
    if not settings.airtrail_token:
        return None
    from adapters.targets.airtrail import AirTrailClient
    return AirTrailClient(settings.airtrail_url, settings.airtrail_token)


def _photon():
    from resolve.geocode import PhotonGeocoder
    return PhotonGeocoder(settings.photon_url)


def _my_traveler(session: Session, request: Request):
    """Whose homes this page shows: the viewer's traveller id — or, for an admin
    acting for someone (?for=, app/acting.py), theirs — or OWNER_HOMES with auth
    off. None = a user with no traveller linked yet."""
    from app.acting import acting_for
    acting = acting_for(session, request)
    request.state.acting = acting
    if acting.owner_mode:
        return OWNER_HOMES
    return acting.traveler.id if acting.traveler else None


def _profiles(session: Session, tid) -> list[HomeProfile]:
    return (home_rows_query(session, tid)
            .order_by(HomeProfile.effective_from.is_(None).desc(),
                      HomeProfile.effective_from, HomeProfile.kind).all())


def _mine(session: Session, tid, profile_id) -> HomeProfile | None:
    """A home by id, only if it's this person's — anyone else's reads as missing."""
    try:
        pid = int(profile_id)
    except (TypeError, ValueError):
        return None
    return home_rows_query(session, tid).filter(HomeProfile.id == pid).one_or_none()


def _page_ctx(session: Session, tid, *, edit: HomeProfile | None = None,
              error: str = "", form: dict | None = None, acting=None) -> dict:
    return {
        "acting": acting,               # admin "whose profile" picker (app/acting.py)
        "no_traveller": tid is None,
        "profiles": _profiles(session, tid),
        "kinds": KINDS,
        "edit_id": edit.id if edit else None,
        "form": form if form is not None else _profile_to_form(edit),
        "error": error,
        "have_airtrail": bool(settings.airtrail_token),
        "have_photon": True,
    }


def _profile_to_form(p: HomeProfile | None) -> dict:
    if p is None:
        return {"kind": "home_airport", "radius_km": "50"}
    return {"label": p.label, "kind": p.kind, "iata": p.iata or "",
            "lat": "" if p.lat is None else p.lat,
            "lon": "" if p.lon is None else p.lon,
            "country": p.country or "", "radius_km": p.radius_km or 50,
            "effective_from": p.effective_from or "",
            "effective_to": p.effective_to or "", "notes": p.notes or ""}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def home_page(request: Request, edit: str | None = None,
              session: Session = Depends(get_session)):
    tid = _my_traveler(session, request)
    editing = _mine(session, tid, edit) if edit else None
    return templates.TemplateResponse(request, "home.html",
                                      _page_ctx(session, tid, edit=editing, acting=request.state.acting))


@router.get("/kind-fields", response_class=HTMLResponse)
def kind_fields(request: Request, kind: str = "home_airport"):
    """Swap the conditional part of the form when the kind changes."""
    return templates.TemplateResponse(request, "_home_kind_fields.html", {
        "form": {"kind": kind, "radius_km": "50"},
        "have_airtrail": bool(settings.airtrail_token), "have_photon": True,
    })


@router.post("/lookup-airport", response_class=HTMLResponse)
def lookup_airport(request: Request, iata: str = Form("")):
    code = (iata or "").strip().upper()
    if not code:
        return _lookup_result(request, error="Enter an airport code first.")
    at = _airtrail()
    if at is not None:
        try:
            a = at.lookup_airport(code)
        except Exception as ex:                 # AirTrail down: fall through
            log.warning("airport lookup failed: %s", ex)
            a = None
        if a is not None:
            return _lookup_result(
                request, found={"label": f"{a.name or code} ({a.iata})",
                                "iata": a.iata, "lat": a.lat, "lon": a.lon,
                                "country": a.country, "source": "AirTrail"})
    # Fallback: Photon for a code AirTrail has never seen flown.
    res = _photon().geocode_freeform(f"{code} airport")
    if res is not None:
        return _lookup_result(
            request, found={"label": f"{code} airport", "iata": code,
                            "lat": round(res.lat, 5), "lon": round(res.lon, 5),
                            "country": (res.country or "").upper(),
                            "source": "Photon (not in AirTrail)"})
    return _lookup_result(request, error=f"No airport found for {code!r}. "
                          "Check the code, or enter coordinates manually.")


@router.post("/lookup-address", response_class=HTMLResponse)
def lookup_address(request: Request, address: str = Form("")):
    res = _photon().geocode_freeform(address)
    if res is None:
        return _lookup_result(request, error="Couldn’t find that address. "
                              "Try a city, or enter coordinates manually.")
    return _lookup_result(
        request, found={"label": res.name or address.strip(),
                        "lat": round(res.lat, 5), "lon": round(res.lon, 5),
                        "country": (res.country or "").upper(),
                        "city": res.city, "source": "Photon"})


def _lookup_result(request: Request, *, found: dict | None = None,
                   error: str = "") -> HTMLResponse:
    return templates.TemplateResponse(request, "_home_lookup_result.html",
                                      {"found": found, "error": error})


def _num(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@router.post("", response_class=HTMLResponse)
@router.post("/", response_class=HTMLResponse)
async def save(request: Request, session: Session = Depends(get_session)):
    tid = _my_traveler(session, request)
    form = dict((await request.form()))
    edit_id = form.get("edit_id")
    kind = form.get("kind") or "home_airport"
    label = (form.get("label") or "").strip()
    iata = (form.get("iata") or "").strip().upper()
    lat, lon = _num(form.get("lat")), _num(form.get("lon"))
    country = (form.get("country") or "").strip().upper() or None

    error = ""
    if tid is None:
        error = ("Your account isn't linked to a traveller yet — link it under "
                 "/account/traveller, or ask an admin.")
    elif not label:
        error = "Give this a name."
    elif kind == "home_airport" and not (len(iata) == 3 and iata.isalpha()):
        error = "A home airport needs a 3-letter airport code (e.g. YYZ)."
    elif kind in ("residence", "home_region") and (lat is None or lon is None):
        error = ("A residence needs coordinates — look up an address, or enter "
                 "latitude and longitude.")
    if error:
        # Fragment, not the full page: this response is swapped into #home-app
        # (hx-swap="outerHTML"). Returning the whole home.html — which extends
        # base.html and so carries the global app header — injected a second
        # header into the body on every save/error (D-39 regression).
        return templates.TemplateResponse(request, "_home_app.html",
                                          _page_ctx(session, tid, error=error, form=form,
                                                    acting=request.state.acting))

    target = (_mine(session, tid, edit_id) if edit_id else None) or HomeProfile()
    # Stamp the owner; with auth off that's the self traveller (or unassigned
    # when there's none yet, the pre-4.1 shape).
    target.traveler_id = self_traveler_id(session) if tid is OWNER_HOMES else tid
    target.label = label
    target.kind = kind
    target.iata = iata if kind == "home_airport" else None
    target.lat, target.lon = lat, lon
    target.country = country
    target.radius_km = _num(form.get("radius_km")) or 50.0
    target.effective_from = (form.get("effective_from") or "").strip() or None
    target.effective_to = (form.get("effective_to") or "").strip() or None
    target.notes = (form.get("notes") or "").strip() or None
    if target.id is None:
        session.add(target)
    from app.acting import audit_if_other
    audit_if_other(session, request, request.state.acting, "home profiles")
    session.commit()
    # Fragment (#home-app): list refreshed, form reset. NOT the full page — the
    # form targets #home-app with hx-swap="outerHTML", so a full-page response
    # would graft a duplicate app header into the DOM each time.
    return templates.TemplateResponse(request, "_home_app.html",
                                      _page_ctx(session, tid, acting=request.state.acting))


@router.post("/delete/{profile_id}", response_class=HTMLResponse)
def delete(profile_id: int, request: Request,
           session: Session = Depends(get_session)):
    tid = _my_traveler(session, request)
    p = _mine(session, tid, profile_id)
    if p is not None:
        from app.acting import audit_if_other
        audit_if_other(session, request, request.state.acting, "home profiles")
        session.delete(p)
        session.commit()
    # Fragment (#home-app), same reason as save(): the delete button swaps this
    # into #home-app, so it must not carry the page header.
    return templates.TemplateResponse(request, "_home_app.html",
                                      _page_ctx(session, tid, acting=request.state.acting))
