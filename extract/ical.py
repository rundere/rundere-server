"""iCalendar (text/calendar) extractor — second in the chain (D-10).

Personal calendar invites are legitimate event items (D-28). This is a small
tolerant VEVENT parser (stdlib only), not a full RFC 5545 implementation: it
handles what Google/Outlook invites actually send — folded lines, TZID
params, VALUE=DATE all-day events, UTC 'Z' times, RRULE presence.
"""
from __future__ import annotations

import re

from extract.windows_tz import to_iana

_FOLD = re.compile(r"\r?\n[ \t]")
_LINE = re.compile(r"^([A-Za-z0-9-]+)((?:;[^:]*)?):(.*)$")
_DT_VALUE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?$")
_TZID = re.compile(r";TZID=([^;:]+)", re.I)
_MAILTO = re.compile(r"(?i)mailto:([^;:\s]+)")


def _unfold(text: str) -> list[str]:
    lines = (ln.strip("\r") for ln in _FOLD.sub("", text).split("\n"))
    return [ln for ln in lines if ln]


def _parse_dt(params: str, value: str) -> dict | None:
    m = _DT_VALUE.match(value.strip())
    if not m:
        return None
    y, mo, d, hh, mm, ss, zulu = m.groups()
    tzm = _TZID.search(params or "")
    if tzm:
        # Google/Apple send IANA names; Outlook/Exchange send Windows names
        # ("Eastern Standard Time"). Map + validate rather than drop — a dropped
        # zone degrades the event to a date-only midnight stub at commit.
        tz = to_iana(tzm.group(1))
    else:
        tz = "Etc/UTC" if zulu else None
    if hh is None:
        return {"local": f"{y}-{mo}-{d}", "precision": "date",
                "tz": tz, "origin": "document"}
    return {"local": f"{y}-{mo}-{d}T{hh}:{mm}", "precision": "datetime",
            "tz": tz, "origin": "document"}


def extract_ical(ical_text: str) -> list[dict]:
    """Returns one event Payload per VEVENT."""
    payloads = []
    in_event = False
    props: dict[str, tuple[str, str]] = {}
    attendees: list[str] = []
    for line in _unfold(ical_text):
        if line.upper() == "BEGIN:VEVENT":
            in_event, props, attendees = True, {}, []
            continue
        if line.upper() == "END:VEVENT":
            in_event = False
            p = _vevent_payload(props, attendees)
            if p:
                payloads.append(p)
            continue
        if not in_event:
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name, params, value = m.group(1).upper(), m.group(2) or "", m.group(3)
        if name == "ATTENDEE":
            em = _MAILTO.search(value) or _MAILTO.search(params)
            attendees.append(em.group(1) if em else value)
        else:
            props[name] = (params, value)


    return payloads


def _unescape(s: str) -> str:
    return (s.replace(r"\n", "\n").replace(r"\,", ",")
             .replace(r"\;", ";").replace("\\\\", "\\"))


def _vevent_payload(props: dict, attendees: list[str]) -> dict | None:
    summary = props.get("SUMMARY", ("", ""))[1]
    dtstart = props.get("DTSTART")
    if not (summary and dtstart):
        return None
    start = _parse_dt(*dtstart)
    if not start:
        return None
    detail: dict = {
        "title": _unescape(summary),
        "start": start,
        "all_day": start["precision"] == "date",
        "gcal_recurring": "RRULE" in props,
        "attendees": attendees,
    }
    if "DTEND" in props:
        end = _parse_dt(*props["DTEND"])
        if end:
            detail["end"] = end
    if "LOCATION" in props and props["LOCATION"][1]:
        loc = _unescape(props["LOCATION"][1])
        detail["venue"] = {"raw": loc, "name": loc}
    if "ORGANIZER" in props:
        params, value = props["ORGANIZER"]
        em = _MAILTO.search(value)
        detail["organizer"] = em.group(1) if em else (value or None)
    if "UID" in props and props["UID"][1]:
        detail["gcal_event_id"] = props["UID"][1]

    status = props.get("STATUS", ("", ""))[1].upper()
    return {
        "item_type": "event",
        # An invite is a plan, not a purchase: planned unless explicitly
        # confirmed/cancelled in the iCal STATUS property.
        "booking_status": {"CONFIRMED": "confirmed",
                           "CANCELLED": "cancelled"}.get(status, "planned"),
        "title": _unescape(summary),
        "event": detail,
    }
