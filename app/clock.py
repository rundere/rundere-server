"""Each person's own "now" (YONDER-219, D-187).

Everything date-relative a person sees — Home's greeting, "in N days"
countdowns, Next up, upcoming/past trips, the Journal's default day — is worked
out against THEIR clock: the time zone on their Travel profile. Not the
server's (it runs on UTC in Docker), and not their home profiles (those change
over time and describe where they live, not the clock they want). With no zone
set, the server clock is used, as before.

Times returned are naive wall-clock values in that zone, because the timeline
stores wall-clock times with no zone (D-172).
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones


@lru_cache(maxsize=1)
def zone_names() -> list[str]:
    """Every IANA zone this server knows, sorted — the Travel profile's picker.
    Legacy aliases ("US/Eastern", "Etc/GMT+5") are left out: they confuse more
    than they help."""
    return sorted(z for z in available_timezones()
                  if "/" in z and not z.startswith(("Etc/", "SystemV/", "US/", "posix/",
                                                    "right/")))


def valid_zone(name: str | None) -> str | None:
    """`name` if it's a real IANA zone, else None."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def zone_for(session, user) -> ZoneInfo | None:
    """The time zone on this person's travel profile, if they set one."""
    tid = getattr(user, "traveler_id", None)
    if tid is None:
        return None
    from app.models import TravelerIdentity
    t = session.get(TravelerIdentity, tid)
    name = valid_zone(getattr(t, "timezone", None))
    return ZoneInfo(name) if name else None


def now_for(session, user, *, _utcnow: datetime | None = None) -> datetime:
    """This person's wall-clock now (naive). The server clock without a zone."""
    tz = zone_for(session, user)
    if tz is None:
        return datetime.now()
    from datetime import timezone
    base = _utcnow or datetime.now(timezone.utc)
    return base.astimezone(tz).replace(tzinfo=None)


def today_for(session, user, **kw) -> str:
    """This person's local date, YYYY-MM-DD."""
    return now_for(session, user, **kw).date().isoformat()
