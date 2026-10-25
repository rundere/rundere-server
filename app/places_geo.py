"""Where a saved place is, in words and in cells (SPEC-11, D-215, D-216).

Pure helpers for the Places Library, with no database and no network:

- `geohash` encodes a pin into the cell string that map clusters group on.
- `fold` is the case- and accent-blind form that search matches on.
- `COUNTRIES` gives each ISO country code its name and continent, for the
  Atlas and for searching "japan".
- `from_airports` is the offline locality guess: the nearest bundled airport's
  city and country (D-146: no Photon needed).
"""
from __future__ import annotations

import math
import unicodedata
from functools import lru_cache

_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash(lat: float, lon: float, precision: int = 9) -> str:
    """The standard geohash of a pin: 9 characters is a cell of about 5 m."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    out, bits, ch, even = [], 0, 0, True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch, lon_lo = (ch << 1) | 1, mid
            else:
                ch, lon_hi = ch << 1, mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch, lat_lo = (ch << 1) | 1, mid
            else:
                ch, lat_hi = ch << 1, mid
        even = not even
        bits += 1
        if bits == 5:
            out.append(_B32[ch])
            bits, ch = 0, 0
    return "".join(out)


def fold(*parts: str | None) -> str:
    """Lower case, no accents, punctuation as spaces: "Café Zürich" and
    "cafe zurich" fold the same. Words are kept apart so a search can match
    each word on its own."""
    text = " ".join(p for p in parts if p)
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in plain).split())


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371000 * 2 * math.asin(math.sqrt(a))


def bbox_around(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) enclosing a circle: the cheap prefilter before
    the exact distance check. Clamped at the poles; not split at the antimeridian
    (a place 25 km across it is missed, which nobody has needed yet)."""
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * max(math.cos(math.radians(lat)), 0.01))
    return (max(lat - dlat, -90), max(lon - dlon, -180),
            min(lat + dlat, 90), min(lon + dlon, 180))


CONTINENTS = {"AF": "Africa", "AN": "Antarctica", "AS": "Asia", "EU": "Europe",
              "NA": "North America", "OC": "Oceania", "SA": "South America"}

# ISO 3166-1 alpha-2 → (continent, name). Transcontinental countries sit where
# travellers usually file them (Türkiye and Russia in Europe, Egypt in Africa).
_BY_CONTINENT = {
    "AF": "AO Angola|BF Burkina Faso|BI Burundi|BJ Benin|BW Botswana|CD DR Congo|"
          "CF Central African Republic|CG Congo|CI Côte d'Ivoire|CM Cameroon|CV Cape Verde|"
          "DJ Djibouti|DZ Algeria|EG Egypt|EH Western Sahara|ER Eritrea|ET Ethiopia|GA Gabon|"
          "GH Ghana|GM Gambia|GN Guinea|GQ Equatorial Guinea|GW Guinea-Bissau|KE Kenya|"
          "KM Comoros|LR Liberia|LS Lesotho|LY Libya|MA Morocco|MG Madagascar|ML Mali|"
          "MR Mauritania|MU Mauritius|MW Malawi|MZ Mozambique|NA Namibia|NE Niger|NG Nigeria|"
          "RE Réunion|RW Rwanda|SC Seychelles|SD Sudan|SH Saint Helena|SL Sierra Leone|"
          "SN Senegal|SO Somalia|SS South Sudan|ST São Tomé and Príncipe|SZ Eswatini|TD Chad|"
          "TG Togo|TN Tunisia|TZ Tanzania|UG Uganda|YT Mayotte|ZA South Africa|ZM Zambia|"
          "ZW Zimbabwe",
    "AN": "AQ Antarctica|BV Bouvet Island|GS South Georgia|HM Heard Island|"
          "TF French Southern Territories",
    "AS": "AE United Arab Emirates|AF Afghanistan|AM Armenia|AZ Azerbaijan|BD Bangladesh|"
          "BH Bahrain|BN Brunei|BT Bhutan|CC Cocos Islands|CN China|CX Christmas Island|"
          "GE Georgia|HK Hong Kong|ID Indonesia|IL Israel|IN India|IO British Indian Ocean "
          "Territory|IQ Iraq|IR Iran|JO Jordan|JP Japan|KG Kyrgyzstan|KH Cambodia|"
          "KP North Korea|KR South Korea|KW Kuwait|KZ Kazakhstan|LA Laos|LB Lebanon|"
          "LK Sri Lanka|MM Myanmar|MN Mongolia|MO Macao|MV Maldives|MY Malaysia|NP Nepal|"
          "OM Oman|PH Philippines|PK Pakistan|PS Palestine|QA Qatar|SA Saudi Arabia|"
          "SG Singapore|SY Syria|TH Thailand|TJ Tajikistan|TL Timor-Leste|TM Turkmenistan|"
          "TW Taiwan|UZ Uzbekistan|VN Vietnam|YE Yemen",
    "EU": "AD Andorra|AL Albania|AT Austria|AX Åland|BA Bosnia and Herzegovina|BE Belgium|"
          "BG Bulgaria|BY Belarus|CH Switzerland|CY Cyprus|CZ Czechia|DE Germany|DK Denmark|"
          "EE Estonia|ES Spain|FI Finland|FO Faroe Islands|FR France|GB United Kingdom|"
          "GG Guernsey|GI Gibraltar|GR Greece|HR Croatia|HU Hungary|IE Ireland|IM Isle of Man|"
          "IS Iceland|IT Italy|JE Jersey|LI Liechtenstein|LT Lithuania|LU Luxembourg|"
          "LV Latvia|MC Monaco|MD Moldova|ME Montenegro|MK North Macedonia|MT Malta|"
          "NL Netherlands|NO Norway|PL Poland|PT Portugal|RO Romania|RS Serbia|RU Russia|"
          "SE Sweden|SI Slovenia|SJ Svalbard and Jan Mayen|SK Slovakia|SM San Marino|"
          "TR Türkiye|UA Ukraine|VA Vatican City|XK Kosovo",
    "NA": "AG Antigua and Barbuda|AI Anguilla|AW Aruba|BB Barbados|BL Saint Barthélemy|"
          "BM Bermuda|BQ Caribbean Netherlands|BS Bahamas|BZ Belize|CA Canada|CR Costa Rica|"
          "CU Cuba|CW Curaçao|DM Dominica|DO Dominican Republic|GD Grenada|GL Greenland|"
          "GP Guadeloupe|GT Guatemala|HN Honduras|HT Haiti|JM Jamaica|KN Saint Kitts and "
          "Nevis|KY Cayman Islands|LC Saint Lucia|MF Saint Martin|MQ Martinique|MS Montserrat|"
          "MX Mexico|NI Nicaragua|PA Panama|PM Saint Pierre and Miquelon|PR Puerto Rico|"
          "SV El Salvador|SX Sint Maarten|TC Turks and Caicos|TT Trinidad and Tobago|"
          "UM US Minor Outlying Islands|US United States|VC Saint Vincent and the Grenadines|"
          "VG British Virgin Islands|VI US Virgin Islands",
    "OC": "AS American Samoa|AU Australia|CK Cook Islands|FJ Fiji|FM Micronesia|GU Guam|"
          "KI Kiribati|MH Marshall Islands|MP Northern Mariana Islands|NC New Caledonia|"
          "NF Norfolk Island|NR Nauru|NU Niue|NZ New Zealand|PF French Polynesia|"
          "PG Papua New Guinea|PN Pitcairn|PW Palau|SB Solomon Islands|TK Tokelau|TO Tonga|"
          "TV Tuvalu|VU Vanuatu|WF Wallis and Futuna|WS Samoa",
    "SA": "AR Argentina|BO Bolivia|BR Brazil|CL Chile|CO Colombia|EC Ecuador|"
          "FK Falkland Islands|GF French Guiana|GY Guyana|PE Peru|PY Paraguay|SR Suriname|"
          "UY Uruguay|VE Venezuela",
}
COUNTRIES: dict[str, tuple[str, str]] = {
    entry[:2]: (cont, entry[3:])
    for cont, entries in _BY_CONTINENT.items() for entry in entries.split("|")}


def country_name(code: str | None) -> str | None:
    c = COUNTRIES.get((code or "").upper())
    return c[1] if c else None


def continent_of(code: str | None) -> str | None:
    c = COUNTRIES.get((code or "").upper())
    return c[0] if c else None


# The offline guess (D-215): a city within this distance of an airport is that
# airport's city; further out, only the country is trusted.
CITY_M = 75_000
COUNTRY_M = 300_000


@lru_cache(maxsize=1)
def _airport_grid() -> dict[tuple[int, int], list]:
    """The bundled airports bucketed by whole degree, read once per process."""
    from resolve import airports
    grid: dict[tuple[int, int], list] = {}
    for a in airports.bundled().values():
        if a.lat is not None and a.lon is not None and a.country:
            grid.setdefault((math.floor(a.lat), math.floor(a.lon)), []).append(a)
    return grid


def from_airports(lat: float, lon: float) -> dict | None:
    """`{locality, country_code}` from the nearest bundled airport, or None when
    no airport is within COUNTRY_M. `locality` is None beyond CITY_M."""
    grid = _airport_grid()
    reach = 3                                   # degrees searched each way (~330 km)
    best = None
    cy, cx = math.floor(lat), math.floor(lon)
    for dy in range(-reach, reach + 1):
        for dx in range(-reach, reach + 1):
            x = (cx + dx + 180) % 360 - 180
            for a in grid.get((cy + dy, x), ()):
                d = metres(lat, lon, a.lat, a.lon)
                if best is None or d < best[0]:
                    best = (d, a)
    if best is None or best[0] > COUNTRY_M:
        return None
    d, a = best
    code = a.country.upper()
    return {"locality": a.municipality if d <= CITY_M and a.municipality else None,
            "country_code": _ALIASES.get(code, code)}


_ALIASES = {"KS": "XK"}     # the airport table's code for Kosovo; ISO uses XK
