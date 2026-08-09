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
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.models import HomeProfile

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


def _profiles(session: Session) -> list[HomeProfile]:
    return (session.query(HomeProfile)
            .order_by(HomeProfile.effective_from.is_(None).desc(),
                      HomeProfile.effective_from, HomeProfile.kind).all())


def _page_ctx(session: Session, *, edit: HomeProfile | None = None,
              error: str = "", form: dict | None = None) -> dict:
    return {
        "profiles": _profiles(session),
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
    editing = session.get(HomeProfile, int(edit)) if edit and edit.isdigit() else None
    return templates.TemplateResponse(request, "home.html",
                                      _page_ctx(session, edit=editing))


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
    form = dict((await request.form()))
    edit_id = form.get("edit_id")
    kind = form.get("kind") or "home_airport"
    label = (form.get("label") or "").strip()
    iata = (form.get("iata") or "").strip().upper()
    lat, lon = _num(form.get("lat")), _num(form.get("lon"))
    country = (form.get("country") or "").strip().upper() or None

    error = ""
    if not label:
        error = "Give this a name."
    elif kind == "home_airport" and not (len(iata) == 3 and iata.isalpha()):
        error = "A home airport needs a 3-letter airport code (e.g. YYZ)."
    elif kind in ("residence", "home_region") and (lat is None or lon is None):
        error = ("A residence needs coordinates — look up an address, or enter "
                 "latitude and longitude.")
    if error:
        return templates.TemplateResponse(request, "home.html",
                                          _page_ctx(session, error=error, form=form))

    target = (session.get(HomeProfile, int(edit_id))
              if edit_id and edit_id.isdigit() else None) or HomeProfile()
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
    session.commit()
    # Full page render: list refreshed, form reset.
    return templates.TemplateResponse(request, "home.html", _page_ctx(session))


@router.post("/delete/{profile_id}", response_class=HTMLResponse)
def delete(profile_id: int, request: Request,
           session: Session = Depends(get_session)):
    p = session.get(HomeProfile, profile_id)
    if p is not None:
        session.delete(p)
        session.commit()
    return templates.TemplateResponse(request, "home.html", _page_ctx(session))
