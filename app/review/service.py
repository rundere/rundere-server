"""Review actions shared by the web queue (app/review/routes.py) and the JSON
API (app/api_v1.py, YONDER-57), so the two surfaces can never disagree on what
an edit, an approval or an ignore does.

Every function takes an item the caller has ALREADY scoped (scope.require_capture
/ capture_or_none) and commits its own changes. Approving only STAGES an item;
nothing here writes to AirTrail or AdventureLog (D-31).
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import TravelItem


class ReviewError(ValueError):
    """A request the item can't take (bad field path, unparseable value…)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark(item: TravelItem, *attrs: str) -> None:
    # JSON columns are not mutation-tracked; be explicit.
    for a in attrs:
        flag_modified(item, a)


def apply_edits(session: Session, item: TravelItem, *, fields: dict[str, str] | None = None,
                passenger_ids: list[str] | None = None,
                traveler_ids: list[int] | None = None,
                strict: bool = False, resolved_by: str = "review-ui") -> list[str]:
    """Apply a reviewer's corrections, then re-resolve. Returns the changed
    payload pointers.

    - `fields`: {json pointer: typed value} for the item's EDITABLE fields
      (routes._fields). Every changed pointer becomes a FIELD LOCK
      (D-13/RULE-15), so a later capture can't silently overwrite it.
    - `passenger_ids`: flights — the chosen Traveller identity ids (never free
      text); [] clears the list. None = unchanged.
    - `traveler_ids`: non-flights — "who is this for", kept on the review.
      None = unchanged.
    `strict` (the API) rejects unknown/non-editable paths, unparseable dates and
    unknown ids with ReviewError; the web form silently skips them as before.
    """
    from app.review.routes import _apply_edit, _fields, _normalize_local, _reresolve
    extraction = dict(item.extraction or {})
    # DEEP copy: a shallow one shares nested dicts with item.extraction, so the
    # edit would mutate the loaded value in place and SQLAlchemy would skip the
    # UPDATE (see routes.save history).
    payload = copy.deepcopy(extraction.get("payload") or {})
    specs = {f["path"]: f for f in _fields(payload) if f.get("editable")}

    changed: list[str] = []
    for path, value in (fields or {}).items():
        spec = specs.get(path)
        if spec is None:
            if strict:
                raise ReviewError(f"not an editable field on this item: {path}")
            continue
        raw = "" if value is None else str(value)
        if strict and spec["kind"] == "datetime" and raw.strip() \
                and _normalize_local(raw) is None:
            raise ReviewError(f"can't read {raw!r} as a date/time for {path} "
                              "(use YYYY-MM-DD or YYYY-MM-DDTHH:MM)")
        if strict and spec["kind"] == "select" and raw and raw not in spec.get("options", []):
            raise ReviewError(f"{path} must be one of {spec.get('options')}")
        if _apply_edit(payload, path, raw, spec["kind"]):
            changed.append(path)

    if passenger_ids is not None:
        from app.resolving import load_identities
        idents = {i.id: i for i in load_identities(session)}
        wanted = [str(i) for i in passenger_ids]
        unknown = [i for i in wanted if i not in idents]
        if strict and payload.get("item_type") != "flight":
            raise ReviewError("passengers apply to flights only; use traveler_ids")
        if strict and unknown:
            raise ReviewError(f"unknown traveller ids: {unknown}")
        chosen = [idents[i] for i in wanted if i in idents]
        new_travelers = [{"raw_name": i.display_name} for i in chosen]
        if new_travelers != (payload.get("travelers") or []):
            payload["travelers"] = new_travelers
            changed.append("/travelers")

    if traveler_ids is not None:
        picked = sorted({int(t) for t in traveler_ids})
        if strict:
            from app.trips import who
            if payload.get("item_type") == "flight":
                raise ReviewError("a flight's travellers are its passengers; use passengers")
            if not item.trip_id:
                raise ReviewError("file the item to a trip first (traveler_ids are that "
                                  "trip's travellers)")
            off = set(picked) - set(who.roster_ids(session, item.trip_id))
            if off:
                raise ReviewError(f"not travellers on this item's trip: {sorted(off)}")
        review = dict(item.review or {})
        if review.get("traveler_ids") != picked:
            review["traveler_ids"] = picked
            item.review = review
            _mark(item, "review")
            session.commit()

    if changed:
        extraction["payload"] = payload
        item.extraction = extraction
        revision = dict(item.revision or {})
        revision["field_locks"] = sorted(set(revision.get("field_locks") or []) | set(changed))
        item.revision = revision
        review = dict(item.review or {})
        review.update({"action_taken": "edited", "resolved_at": _now(),
                       "resolved_by": resolved_by})
        item.review = review
        status = dict(item.status or {})
        status["resolve"] = "pending"        # re-run resolution on the corrected payload
        item.status = status
        _mark(item, "extraction", "revision", "review", "status")
        session.commit()
        _reresolve(session, item)
    return changed


def approve(session: Session, item: TravelItem, *, resolved_by: str = "review-ui") -> bool:
    """Stage the item for logging. Only a RESOLVED item (resolve == ok) stages;
    an item that was never resolved is re-resolved first. Returns whether it
    was approved (False = still needs attention; see its reasons)."""
    from app.review.routes import _reresolve
    if not (item.resolution or {}).get("outcome"):
        _reresolve(session, item)
        session.refresh(item)
    if (item.status or {}).get("resolve") != "ok":
        return False
    review = dict(item.review or {})
    review.update({"required": False, "action_taken": "approved",
                   "resolved_at": _now(), "resolved_by": resolved_by})
    item.review = review
    status = dict(item.status or {})
    status["resolve"] = "ok"
    item.status = status
    _mark(item, "review", "status")
    session.commit()
    return True


def unapprove(session: Session, item: TravelItem) -> bool:
    """Take back an approval that hasn't been logged yet, and re-resolve so the
    item returns to its natural bucket. A LOGGED item can't be un-approved (the
    write already happened). Returns whether it was un-approved."""
    from app.review.routes import _is_logged, _reresolve
    if _is_logged(item):
        return False
    review = dict(item.review or {})
    for k in ("action_taken", "resolved_at", "resolved_by"):
        review.pop(k, None)
    item.review = review
    _mark(item, "review")
    session.commit()
    _reresolve(session, item)
    return True


def ignore(session: Session, item: TravelItem, *, resolved_by: str = "review-ui") -> None:
    """Take the item off the pipeline (terminal): it writes nothing."""
    review = dict(item.review or {})
    review.update({"required": False, "action_taken": "ignored",
                   "resolved_at": _now(), "resolved_by": resolved_by})
    resolution = dict(item.resolution or {})
    resolution["outcome"] = "ignored"
    resolution["targets"] = []
    status = dict(item.status or {})
    status["resolve"] = "skipped"
    item.review, item.resolution, item.status = review, resolution, status
    _mark(item, "review", "resolution", "status")
    session.commit()
