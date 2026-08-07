"""Google Calendar event extractor — the `gcal` capture channel.

The Calendar add-on (`capture/gcal-addon/`) sends the Google Calendar API *Event
resource* verbatim as JSON, rather than reconstructing an .ics client-side: the
mapping to our event Payload then lives HERE, in Python, where it is unit-tested,
instead of in Apps Script where it is not.

The output is the SAME shape `extract/ical.py` produces for a VEVENT, so an event
captured from Gmail (an .ics invite) and one captured from Google Calendar resolve
and commit through identical downstream code. Google Calendar always uses IANA
zone names (`America/New_York`), so no Windows-name mapping is needed here — but we
still validate through the shared resolver so junk never survives (see D-56).
"""
from __future__ import annotations

from extract.windows_tz import to_iana

_STATUS = {"confirmed": "confirmed", "cancelled": "cancelled",
           "tentative": "planned"}


def _when(node: dict | None) -> dict | None:
    """A Calendar API start/end ({dateTime,timeZone} or {date}) -> DateTimeLocal.

    Timed: `dateTime` is RFC 3339 ("2026-09-01T16:00:00-04:00"); the local
    wall-clock is its first 16 chars and the zone is the sibling `timeZone`
    (IANA). All-day: `date` ("2026-09-01") -> precision date, no zone. NEVER
    invents a 00:00 (D-06) — an all-day event stays date-precision."""
    if not isinstance(node, dict):
        return None
    dt = node.get("dateTime")
    if dt:
        local = str(dt)[:16]
        if len(local) < 16 or "T" not in local:
            return None
        tz = to_iana(node.get("timeZone")) or ("Etc/UTC" if str(dt).endswith("Z") else None)
        return {"local": local, "precision": "datetime", "tz": tz,
                "origin": "document"}
    d = node.get("date")
    if d:
        return {"local": str(d)[:10], "precision": "date", "tz": None,
                "origin": "document"}
    return None


def extract_gcal_event(event: dict) -> list[dict]:
    """A Calendar API Event resource -> [event Payload]. Returns [] when the
    event lacks the minimum (a title and a start), matching ical.py's tolerance."""
    if not isinstance(event, dict):
        return []
    summary = (event.get("summary") or "").strip()
    start = _when(event.get("start"))
    if not summary or not start:
        return []

    detail: dict = {
        "title": summary,
        "start": start,
        "all_day": start["precision"] == "date",
        "gcal_recurring": bool(event.get("recurrence")),
        "attendees": [a.get("email") for a in (event.get("attendees") or [])
                      if isinstance(a, dict) and a.get("email")],
    }
    end = _when(event.get("end"))
    if end:
        detail["end"] = end
    location = (event.get("location") or "").strip()
    if location:
        detail["venue"] = {"raw": location, "name": location}
    organizer = event.get("organizer") or {}
    if isinstance(organizer, dict) and organizer.get("email"):
        detail["organizer"] = organizer["email"]
    # iCalUID is stable across a recurring series and across Gmail-vs-Calendar
    # capture of the same invite, so it is the right identity anchor (matches
    # ical.py using the VEVENT UID). Fall back to the per-instance id.
    uid = event.get("iCalUID") or event.get("id")
    if uid:
        detail["gcal_event_id"] = str(uid)

    status = str(event.get("status") or "").lower()
    return [{
        "item_type": "event",
        # An invite is a plan unless the calendar says otherwise (mirrors ical.py).
        "booking_status": _STATUS.get(status, "planned"),
        "title": summary,
        "event": detail,
    }]
