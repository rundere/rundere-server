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


def zone_options() -> list[tuple[str, str]]:
    """(zone, label) for the Travel profile's picker, ordered by UTC offset,
    e.g. ("Asia/Kolkata", "(UTC+05:30) Kolkata — India Time (IST)"). The raw
    IANA name meant nothing to people (YONDER-321); the saved value stays the
    IANA zone. Offsets are today's (daylight saving), so it's worked out daily."""
    return _zone_options(datetime.now(ZoneInfo("UTC")).date().isoformat())


@lru_cache(maxsize=2)
def _zone_options(_day: str) -> list[tuple[str, str]]:
    from babel.dates import get_timezone, get_timezone_name
    rows = []
    for zone in zone_names():
        try:
            now = datetime.now(ZoneInfo(zone))
            off = now.utcoffset()
        except (ZoneInfoNotFoundError, ValueError, OSError):   # a broken zone is skipped
            continue
        mins = int(off.total_seconds() // 60) if off is not None else 0
        sign = "+" if mins >= 0 else "−"
        offset = f"UTC{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"
        city = zone.rsplit("/", 1)[-1].replace("_", " ")
        try:
            btz = get_timezone(zone)
            name = get_timezone_name(datetime.now(btz), width="long", locale="en")
            if name.startswith("GMT"):        # no specific name: use the region's
                name = get_timezone_name(btz, width="long", locale="en")
                name = name.split(" (", 1)[0] + (" Time" if " (" in name else "")
        except Exception:                     # noqa: BLE001 - the city alone still helps
            name = ""
        abbr = now.tzname() or ""
        tail = f" ({abbr})" if abbr.isalpha() and abbr not in name else ""
        label = f"({offset}) {city}" + (f" — {name}{tail}" if name else tail)
        rows.append((mins, label, zone))
    return [(zone, label) for _, label, zone in sorted(rows)]


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
