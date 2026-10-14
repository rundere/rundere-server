"""What's next, and what's under way, across the viewer's timeline (YONDER-177, D-172).

Home's Next-up card and `/api/v1/dashboard` both ask the same question, so the
rule lives here once:

- Every timed activity is one or two **moments**: its start (`when`) and, when it
  has one, its end (`endWhen`). A car rental is a pick-up and a drop-off; a hotel
  is a check-in and a check-out.
- **Next up** is the soonest moment still ahead. A multi-day booking therefore
  never holds the card for its whole span: once the rental is picked up, the
  hotel check-in two hours later is next, then the check-out, and the drop-off
  only when nothing comes before it.
- **In progress** is what has started and not ended (the rental, the stay). It
  is shown alongside the card, never as the card.

Times are the activity's local wall clock ("YYYY-MM-DDTHH:MM", no zone), as the
timeline stores them, so "now" is a wall-clock string too: the caller's (the
app sends its device time) or the server's. A date with no time counts from the
start of that day; an end date with no time runs to the end of that day. An item
with a start time and no end is a single moment, so it simply drops off once it
starts; an all-day item (date only, no end) is under way for that day. Notes are
reference text, not bookings, so they're never next up.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

_SKIP_TYPES = {"note"}
# Start labels that read badly as "NEXT UP · …"; the type's own label is used
# instead ("Restaurant", "Tour / activity").
_GENERIC_START = {"Start", "Reservation time", "Date (optional)"}


@dataclass(frozen=True)
class Moment:
    activity: Any
    edge: str          # "start" | "end"
    at: str            # as stored: "YYYY-MM-DDTHH:MM" or "YYYY-MM-DD"
    key: str           # sortable "YYYY-MM-DDTHH:MM"


def now_key(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%dT%H:%M")


def _norm(raw: Any, *, end: bool = False) -> tuple[str, str] | None:
    """(as stored, sortable key) for a timeline time, or None when it isn't one.
    Seconds and any zone suffix are dropped: the timeline is wall-clock time."""
    s = str(raw or "").strip()
    try:
        date.fromisoformat(s[:10])
    except ValueError:
        return None
    if len(s) >= 16 and s[10] in "T " and s[13] == ":":
        hm = s[11:16]
        return s[:10] + "T" + hm, s[:10] + "T" + hm
    return s[:10], s[:10] + ("T23:59" if end else "T00:00")


def _span(a) -> tuple[tuple[str, str], tuple[str, str] | None, bool] | None:
    """(start, end, end_is_explicit) for an activity; None when it has no time."""
    v = a.values or {}
    start = _norm(a.starts_at or v.get("when"))
    if start is None:
        return None
    end = _norm(v.get("endWhen"), end=True)
    if end is not None and end[1] > start[1]:
        return start, end, True
    if len(start[0]) == 10:                       # all day: under way for that day
        return start, (start[0], start[0] + "T23:59"), False
    return start, None, False


def _moments(activities, now: str) -> tuple[list[Moment], list[Moment]]:
    """(every moment still ahead, what's in progress) at `now`, both soonest first."""
    ahead: list[Moment] = []
    under_way: list[Moment] = []
    for a in activities:
        if a.type in _SKIP_TYPES:
            continue
        span = _span(a)
        if span is None:
            continue
        (s_at, s_key), end, explicit = span
        if s_key > now:
            ahead.append(Moment(a, "start", s_at, s_key))
        elif end is not None and now < end[1]:
            under_way.append(Moment(a, "end", end[0], end[1]))
        if explicit and end[1] > now:
            ahead.append(Moment(a, "end", end[0], end[1]))
    # Ties: an end before a start — you check out, then drive away.
    ahead.sort(key=lambda m: (m.key, 0 if m.edge == "end" else 1))
    under_way.sort(key=lambda m: m.key)
    return ahead, under_way


def plan(activities, now: str) -> tuple[Moment | None, list[Moment]]:
    """(next moment, what's in progress) for `activities` at wall-clock `now`
    ("YYYY-MM-DDTHH:MM"). In-progress moments are each item's end, soonest first,
    without the item the next moment belongs to (it's already on the card)."""
    ahead, under_way = _moments(activities, now)
    nxt = ahead[0] if ahead else None
    if nxt is not None:
        under_way = [m for m in under_way if m.activity is not nxt.activity]
    return nxt, under_way


def upcoming(activities, now: str, *, window_h: int, limit: int) -> list[Moment]:
    """The next `limit` moments within `window_h` hours of `now`, soonest first
    (YONDER-250, SPEC-11 §6). The first is always `plan()`'s next moment, so a
    list that rolls forward (the Apple Watch) agrees with the Next-up card. The
    window is wall-clock hours, like everything here."""
    from datetime import timedelta
    until = (datetime.strptime(now, "%Y-%m-%dT%H:%M")
             + timedelta(hours=window_h)).strftime("%Y-%m-%dT%H:%M")
    ahead, _ = _moments(activities, now)
    return [m for m in ahead if m.key <= until][:limit]


def label(m: Moment) -> str:
    """What the moment is called: "Check-in", "Drop-off", "Departure", or the
    type's own name ("Restaurant") when its start label is generic."""
    from app.trips import types as activity_types
    start_label, end_label = activity_types.time_labels(m.activity.type)
    if m.edge == "end":
        return end_label or "Ends"
    if start_label and start_label not in _GENERIC_START:
        return start_label
    return activity_types.get_type(m.activity.type).label
