"""Windows/Outlook time-zone names → IANA, plus tolerant tz resolution.

Outlook/Exchange calendar invites (and the .ics they emit) label times with CLDR
"Windows" zone names — `TZID=Eastern Standard Time` — instead of the IANA name
`America/New_York`. Google Calendar and Apple Calendar already use IANA names, so
those pass straight through. This module maps the Windows names to their CLDR
primary-territory ("001") IANA zone and validates ANY candidate against the tz
database, so a name we don't recognise (or genuine junk) resolves to None rather
than a bogus zone.

Why this exists: `extract/ical.py` used to DROP every non-IANA TZID, silently
losing the zone on Outlook-origin invites. With no tz, the commit stage cannot
build a UTC time and degrades the event to a date-only midnight stub — the real
4 pm start is lost. Mapping instead of dropping keeps the time.

Mapping source: Unicode CLDR `windowsZones.xml` (territory 001 defaults). Stable
IANA links (e.g. Asia/Kolkata, Europe/Kiev) are used deliberately so validation
succeeds across tzdata versions.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

WINDOWS_TO_IANA: dict[str, str] = {
    # --- UTC / dateline ---
    "Dateline Standard Time": "Etc/GMT+12",
    "UTC-11": "Etc/GMT+11",
    "UTC-09": "Etc/GMT+9",
    "UTC-08": "Etc/GMT+8",
    "UTC-02": "Etc/GMT+2",
    "UTC": "Etc/UTC",
    "UTC+12": "Etc/GMT-12",
    "UTC+13": "Etc/GMT-13",
    "Mid-Atlantic Standard Time": "Etc/GMT+2",
    # --- North America ---
    "Aleutian Standard Time": "America/Adak",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Marquesas Standard Time": "Pacific/Marquesas",
    "Alaskan Standard Time": "America/Anchorage",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time (Mexico)": "America/Mazatlan",
    "Mountain Standard Time": "America/Denver",
    "Central America Standard Time": "America/Guatemala",
    "Central Standard Time": "America/Chicago",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina",
    "Eastern Standard Time (Mexico)": "America/Cancun",
    "Eastern Standard Time": "America/New_York",
    "US Eastern Standard Time": "America/Indianapolis",
    "Atlantic Standard Time": "America/Halifax",
    "Newfoundland Standard Time": "America/St_Johns",
    "Haiti Standard Time": "America/Port-au-Prince",
    "Cuba Standard Time": "America/Havana",
    "Turks And Caicos Standard Time": "America/Grand_Turk",
    "Greenland Standard Time": "America/Godthab",
    "Saint Pierre Standard Time": "America/Miquelon",
    # --- Central & South America ---
    "SA Pacific Standard Time": "America/Bogota",
    "Easter Island Standard Time": "Pacific/Easter",
    "Venezuela Standard Time": "America/Caracas",
    "Paraguay Standard Time": "America/Asuncion",
    "Central Brazilian Standard Time": "America/Cuiaba",
    "SA Western Standard Time": "America/La_Paz",
    "Pacific SA Standard Time": "America/Santiago",
    "Tocantins Standard Time": "America/Araguaina",
    "E. South America Standard Time": "America/Sao_Paulo",
    "SA Eastern Standard Time": "America/Cayenne",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "Montevideo Standard Time": "America/Montevideo",
    "Magallanes Standard Time": "America/Punta_Arenas",
    "Bahia Standard Time": "America/Bahia",
    # --- Atlantic / Europe / Africa ---
    "Azores Standard Time": "Atlantic/Azores",
    "Cape Verde Standard Time": "Atlantic/Cape_Verde",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Sao Tome Standard Time": "Africa/Sao_Tome",
    "Morocco Standard Time": "Africa/Casablanca",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "GTB Standard Time": "Europe/Bucharest",
    "Middle East Standard Time": "Asia/Beirut",
    "Egypt Standard Time": "Africa/Cairo",
    "E. Europe Standard Time": "Europe/Chisinau",
    "Syria Standard Time": "Asia/Damascus",
    "West Bank Standard Time": "Asia/Hebron",
    "South Africa Standard Time": "Africa/Johannesburg",
    "FLE Standard Time": "Europe/Kiev",
    "Israel Standard Time": "Asia/Jerusalem",
    "Kaliningrad Standard Time": "Europe/Kaliningrad",
    "Sudan Standard Time": "Africa/Khartoum",
    "Libya Standard Time": "Africa/Tripoli",
    "Namibia Standard Time": "Africa/Windhoek",
    "Jordan Standard Time": "Asia/Amman",
    "Turkey Standard Time": "Europe/Istanbul",
    "Belarus Standard Time": "Europe/Minsk",
    "Russian Standard Time": "Europe/Moscow",
    # --- Middle East / Central & South Asia ---
    "Arabic Standard Time": "Asia/Baghdad",
    "Arab Standard Time": "Asia/Riyadh",
    "E. Africa Standard Time": "Africa/Nairobi",
    "Iran Standard Time": "Asia/Tehran",
    "Arabian Standard Time": "Asia/Dubai",
    "Astrakhan Standard Time": "Europe/Astrakhan",
    "Azerbaijan Standard Time": "Asia/Baku",
    "Russia Time Zone 3": "Europe/Samara",
    "Mauritius Standard Time": "Indian/Mauritius",
    "Saratov Standard Time": "Europe/Saratov",
    "Georgian Standard Time": "Asia/Tbilisi",
    "Volgograd Standard Time": "Europe/Volgograd",
    "Caucasus Standard Time": "Asia/Yerevan",
    "Afghanistan Standard Time": "Asia/Kabul",
    "West Asia Standard Time": "Asia/Tashkent",
    "Ekaterinburg Standard Time": "Asia/Yekaterinburg",
    "Pakistan Standard Time": "Asia/Karachi",
    "India Standard Time": "Asia/Kolkata",
    "Sri Lanka Standard Time": "Asia/Colombo",
    "Nepal Standard Time": "Asia/Kathmandu",
    "Central Asia Standard Time": "Asia/Almaty",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "Omsk Standard Time": "Asia/Omsk",
    "Myanmar Standard Time": "Asia/Yangon",
    # --- East & Southeast Asia ---
    "SE Asia Standard Time": "Asia/Bangkok",
    "Altai Standard Time": "Asia/Barnaul",
    "W. Mongolia Standard Time": "Asia/Hovd",
    "North Asia Standard Time": "Asia/Krasnoyarsk",
    "N. Central Asia Standard Time": "Asia/Novosibirsk",
    "Tomsk Standard Time": "Asia/Tomsk",
    "China Standard Time": "Asia/Shanghai",
    "North Asia East Standard Time": "Asia/Irkutsk",
    "Singapore Standard Time": "Asia/Singapore",
    "Taipei Standard Time": "Asia/Taipei",
    "Ulaanbaatar Standard Time": "Asia/Ulaanbaatar",
    "Transbaikal Standard Time": "Asia/Chita",
    "Tokyo Standard Time": "Asia/Tokyo",
    "North Korea Standard Time": "Asia/Pyongyang",
    "Korea Standard Time": "Asia/Seoul",
    "Yakutsk Standard Time": "Asia/Yakutsk",
    # --- Australia / Pacific ---
    "W. Australia Standard Time": "Australia/Perth",
    "Aus Central W. Standard Time": "Australia/Eucla",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin",
    "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Tasmania Standard Time": "Australia/Hobart",
    "Lord Howe Standard Time": "Australia/Lord_Howe",
    "West Pacific Standard Time": "Pacific/Port_Moresby",
    "Vladivostok Standard Time": "Asia/Vladivostok",
    "Bougainville Standard Time": "Pacific/Bougainville",
    "Russia Time Zone 10": "Asia/Srednekolymsk",
    "Magadan Standard Time": "Asia/Magadan",
    "Norfolk Standard Time": "Pacific/Norfolk",
    "Sakhalin Standard Time": "Asia/Sakhalin",
    "Central Pacific Standard Time": "Pacific/Guadalcanal",
    "Russia Time Zone 11": "Asia/Kamchatka",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Fiji Standard Time": "Pacific/Fiji",
    "Chatham Islands Standard Time": "Pacific/Chatham",
    "Tonga Standard Time": "Pacific/Tongatapu",
    "Samoa Standard Time": "Pacific/Apia",
    "Line Islands Standard Time": "Pacific/Kiritimati",
}


def to_iana(tzid: str | None) -> str | None:
    """Resolve a calendar TZID to a validated IANA zone name, or None.

    - Outlook/Windows names (`Eastern Standard Time`) map via the curated table.
    - Google/Apple IANA names (`America/New_York`, incl. multi-segment ones like
      `America/Argentina/Buenos_Aires`) pass through after validation.
    - Anything the tz database does not know resolves to None — never a guess.
    """
    if not tzid:
        return None
    name = tzid.strip().strip('"').strip()
    if not name:
        return None
    name = WINDOWS_TO_IANA.get(name, name)
    try:
        ZoneInfo(name)                       # the tz database is the arbiter
    except Exception:
        return None
    return name
