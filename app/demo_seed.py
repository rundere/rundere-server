"""The demo server's sample data and its nightly roll-forward (YONDER-181, D-198).

`seed()` writes three fictional trips for the demo user, in the shapes the app
itself writes. Each is dated relative to today, so there's always one trip under
way, one coming up and one in the past:

- **Lisbon & Porto**, under way (day 2 of 5): a timeline with moments in the
  next 48 hours (Today, the Watch, the Live Activity), the full Discover set,
  costs in euros.
- **Banff long weekend**, about three weeks out: a road trip with a route
  line, Discover and a generated packing plan.
- **New York City**, about two months ago: journal entries, costs split with a
  companion, and a plan against actuals.

Plus two captures waiting in Review, for a trip that isn't planned yet.

Everything is written through the app's own validated paths (the agent's
`save_research_finding`, expenses, budget, journal, the packing generator and
the capture pipeline), so the demo shows exactly what a real account would.
They run inside `demo.seeding()`, the one narrow lift of the read-only rule.
Nothing calls out: places come from `SeedGeocoder`, and the two public feeds
the deterministic Discover topics read (Frankfurter, Open-Meteo) are answered
by `offline_feeds()` from fixed rates and rough climate averages. The demo
banner already says this is sample data.

`roll()` keeps it current without wiping anything. It finds how far the
in-progress trip's start has drifted from where the seed puts it, shifts every
seeded date by that many days, refreshes the weather and currency findings for
the new dates, and regenerates the waiting captures. Running it twice changes
nothing the second time. `start()` runs it at startup and then daily at 03:00 in
`TX_DEMO_TIMEZONE`.
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import settings

log = logging.getLogger("rundere.demo")

TAG = "demo:"                     # Trip.external_ref of every seeded trip
ANCHOR_TRIP = "demo:lisbon"       # the in-progress trip the roll measures from
ANCHOR_OFFSET = -1                # …whose start is yesterday


# --------------------------------------------------------------------------- places
# Where the seed's places are, so nothing is geocoded at seed or read time.
PLACES = {
    "lisbon": ("PT", "Lisbon", 38.7223, -9.1393),
    "porto": ("PT", "Porto", 41.1579, -8.6291),
    "banff": ("CA", "Banff", 51.1784, -115.5708),
    "lake louise": ("CA", "Lake Louise", 51.4254, -116.1773),
    "calgary": ("CA", "Calgary", 51.0447, -114.0719),
    "new york": ("US", "New York", 40.7128, -74.0060),
    "manhattan": ("US", "New York", 40.7831, -73.9712),
    "tokyo": ("JP", "Tokyo", 35.6762, 139.6503),
}


class SeedGeocoder:
    """A geocoder that knows only the seed's cities (same interface as
    resolve.geocode.PhotonGeocoder). Used by the seed and, on a demo server, by
    reads that place a trip (`app/trips/routes._photon`)."""

    def _match(self, *texts):
        from resolve.geocode import GeocodeResult
        blob = " ".join(t for t in texts if t).lower()
        for key, (country, city, lat, lon) in PLACES.items():
            if key in blob:
                return GeocodeResult(lat=lat, lon=lon, country=country, city=city,
                                     name=city, confidence="verified", precision="area")
        return None

    def geocode_place(self, place: dict):
        return self._match(place.get("name"), place.get("raw"), place.get("city"),
                           place.get("address"))

    def geocode_freeform(self, text: str):
        return self._match(text)


# --------------------------------------------------------------------------- feeds
# 1 unit of the key = this many of each quote (fixed, rounded, for a demo).
_RATES = {("CAD", "EUR"): 0.66, ("EUR", "CAD"): 1.51, ("CAD", "USD"): 0.72,
          ("USD", "CAD"): 1.38, ("EUR", "USD"): 1.09, ("USD", "EUR"): 0.92,
          ("CAD", "JPY"): 108.0, ("JPY", "CAD"): 0.0093}
# Rough daily highs/lows (°C), rain (mm/day), humidity (%) and elevation (m) by
# city. Approximations for a showcase, not records.
_CLIMATE = {
    "Lisbon": (24, 15, 1.2, 65, 50), "Porto": (21, 13, 2.5, 72, 90),
    "Banff": (16, 2, 1.8, 60, 1384), "Lake Louise": (14, 0, 2.0, 62, 1540),
    "Calgary": (17, 4, 1.5, 55, 1045), "New York": (20, 12, 3.0, 64, 10),
}


def _nearest_city(lat: float, lon: float) -> str:
    best, dist = "Lisbon", 1e9
    for _, (_, city, plat, plon) in PLACES.items():
        d = (plat - lat) ** 2 + (plon - lon) ** 2
        if city in _CLIMATE and d < dist:
            best, dist = city, d
    return best


def _fake_get_json(url: str, timeout: int = 10, **_):
    """Answer the Frankfurter and Open-Meteo calls the deterministic Discover
    resolvers make, offline."""
    from urllib.parse import parse_qs, urlparse
    u = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if u.path.endswith("/rates"):                                  # Frankfurter v2
        base, quote = q.get("base", ""), q.get("quotes", "")
        rate = _RATES.get((base, quote))
        if rate is None:
            return []
        return [{"date": date.today().isoformat(), "base": base, "quote": quote, "rate": rate}]
    if "daily" in q and "start_date" in q:                         # Open-Meteo
        city = _nearest_city(float(q.get("latitude", 0)), float(q.get("longitude", 0)))
        hi, lo, rain, rh, elev = _CLIMATE[city]
        start, end = date.fromisoformat(q["start_date"]), date.fromisoformat(q["end_date"])
        days = [(start + timedelta(days=i)) for i in range((end - start).days + 1)]
        series = {
            "temperature_2m_max": [hi + (i % 3) - 1 for i in range(len(days))],
            "temperature_2m_min": [lo + (i % 2) for i in range(len(days))],
            "precipitation_sum": [round(rain * ((i % 4) / 2), 1) for i in range(len(days))],
            "apparent_temperature_max": [hi + (i % 3) - 2 for i in range(len(days))],
            "apparent_temperature_min": [lo - 1 for _ in days],
            "relative_humidity_2m_mean": [rh for _ in days],
            "relative_humidity_2m_min": [rh - 15 for _ in days],
            "relative_humidity_2m_max": [rh + 15 for _ in days],
        }
        wanted = q["daily"].split(",")
        daily = {"time": [d.isoformat() for d in days]}
        daily.update({k: v for k, v in series.items() if k in wanted})
        return {"latitude": q.get("latitude"), "longitude": q.get("longitude"),
                "elevation": elev, "daily": daily}
    return None


@contextlib.contextmanager
def offline_feeds():
    """Point the currency and weather resolvers at `_fake_get_json`."""
    from discover.resolvers import currency, weather
    saved = currency.get_json, weather.get_json
    currency.get_json = weather.get_json = _fake_get_json
    try:
        yield
    finally:
        currency.get_json, weather.get_json = saved


# --------------------------------------------------------------------------- data
def _pt(role, label, place):
    _, _, lat, lon = PLACES[place] if isinstance(place, str) else (None, None, *place)
    return {"role": role, "label": label, "lat": lat, "lon": lon, "source": "link"}


# A trip: key, name, start/end offsets (days from today), notes, who's on it,
# timeline activities as (type, start (day, "HH:MM"), end or None, values, points).
TRIPS = [
    {"key": "lisbon", "name": "Lisbon & Porto", "start": -1, "end": 3,
     "notes": "Sample trip on the Run·dere demo server. Five days: Lisbon, then the "
              "train north to Porto.",
     "companion": True, "currency": "CAD",
     "activities": [
         ("flight", (-2, "21:15"), (-1, "09:05"),
          {"airline": "Air Canada", "flightNumber": "AC 832", "from": "YYZ", "to": "LIS",
           "confirmation": "DEMO-LX4"},
          [_pt("from", "YYZ", (43.6777, -79.6248)), _pt("to", "LIS", (38.7742, -9.1342))]),
         ("lodging", (-1, "14:00"), (2, "11:00"),
          {"name": "Casa do Miradouro (sample)", "type": "Hotel",
           "address": "Rua da Rosa, Bairro Alto, Lisbon", "confirmation": "DEMO-CM19"},
          [_pt("place", "Casa do Miradouro", (38.7128, -9.1446))]),
         ("dining", (0, "20:00"), None,
          {"name": "Tasca do Largo (sample)", "address": "Bairro Alto, Lisbon", "partySize": "2"},
          [_pt("place", "Tasca do Largo", (38.7120, -9.1452))]),
         ("tour", (1, "10:00"), (1, "13:00"),
          {"name": "Belém monuments walk", "provider": "Sample Walks Lisbon",
           "location": "Mosteiro dos Jerónimos, Lisbon", "duration": "3 h"},
          [_pt("place", "Jerónimos Monastery", (38.6979, -9.2068))]),
         ("train", (2, "11:39"), (2, "14:28"),
          {"operator": "CP Alfa Pendular", "trainNumber": "AP 133",
           "from": "Lisbon Santa Apolónia", "to": "Porto Campanhã", "confirmation": "DEMO-AP3"},
          [_pt("from", "Lisbon Santa Apolónia", (38.7139, -9.1225)),
           _pt("to", "Porto Campanhã", (41.1486, -8.5853))]),
         ("lodging", (2, "15:30"), (3, "10:00"),
          {"name": "Ribeira Guesthouse (sample)", "type": "Guesthouse",
           "address": "Ribeira, Porto", "confirmation": "DEMO-RG7"},
          [_pt("place", "Ribeira Guesthouse", (41.1406, -8.6131))]),
         ("dining", (2, "20:30"), None,
          {"name": "Adega da Ribeira (sample)", "address": "Ribeira, Porto", "partySize": "2"},
          [_pt("place", "Adega da Ribeira", (41.1411, -8.6120))]),
         ("flight", (3, "13:35"), (3, "16:50"),
          {"airline": "Air Canada", "flightNumber": "AC 877", "from": "OPO", "to": "YYZ",
           "confirmation": "DEMO-LX4"},
          [_pt("from", "OPO", (41.2481, -8.6814)), _pt("to", "YYZ", (43.6777, -79.6248))]),
     ]},
    {"key": "banff", "name": "Banff long weekend", "start": 21, "end": 24,
     "notes": "Sample trip: fly to Calgary, drive into the Rockies, two days of lakes "
              "and trails.",
     "companion": False, "currency": "CAD",
     "activities": [
         ("flight", (21, "08:00"), (21, "10:20"),
          {"airline": "WestJet", "flightNumber": "WS 663", "from": "YYZ", "to": "YYC",
           "confirmation": "DEMO-WB2"},
          [_pt("from", "YYZ", (43.6777, -79.6248)), _pt("to", "YYC", (51.1215, -114.0076))]),
         ("carRental", (21, "11:00"), (24, "12:00"),
          {"company": "Sample Car Rental", "pickupLocation": "Calgary Airport (YYC)",
           "dropoffLocation": "Calgary Airport (YYC)", "confirmation": "DEMO-CR5"},
          [_pt("from", "YYC", (51.1215, -114.0076)), _pt("to", "YYC", (51.1215, -114.0076))]),
         ("drive", (21, "11:30"), (21, "13:15"),
          {"route": "Calgary Airport → Banff", "from": "Calgary Airport", "to": "Banff",
           "distance": "145 km"},
          [_pt("from", "Calgary Airport", (51.1215, -114.0076)),
           _pt("to", "Banff", "banff")]),
         ("lodging", (21, "15:00"), (24, "10:00"),
          {"name": "Cascade Lodge (sample)", "type": "Lodge",
           "address": "Banff Avenue, Banff, AB", "confirmation": "DEMO-CL8"},
          [_pt("place", "Cascade Lodge", (51.1802, -115.5683))]),
         ("tour", (22, "08:00"), (22, "14:00"),
          {"name": "Lake Louise & Moraine Lake", "location": "Lake Louise, AB",
           "duration": "6 h"},
          [_pt("place", "Lake Louise", "lake louise")]),
         ("tour", (23, "10:00"), (23, "12:30"),
          {"name": "Sulphur Mountain gondola", "location": "Banff, AB", "duration": "2.5 h"},
          [_pt("place", "Banff Gondola", (51.1441, -115.5735))]),
         ("drive", (24, "10:15"), (24, "11:50"),
          {"route": "Banff → Calgary Airport", "from": "Banff", "to": "Calgary Airport",
           "distance": "145 km"},
          [_pt("from", "Banff", "banff"), _pt("to", "Calgary Airport", (51.1215, -114.0076))]),
         ("flight", (24, "14:00"), (24, "19:45"),
          {"airline": "WestJet", "flightNumber": "WS 668", "from": "YYC", "to": "YYZ",
           "confirmation": "DEMO-WB2"},
          [_pt("from", "YYC", (51.1215, -114.0076)), _pt("to", "YYZ", (43.6777, -79.6248))]),
     ]},
    {"key": "nyc", "name": "New York City", "start": -62, "end": -58,
     "notes": "Sample trip from two months ago: a long weekend in Manhattan.",
     "companion": True, "currency": "CAD",
     "activities": [
         ("flight", (-62, "07:30"), (-62, "09:00"),
          {"airline": "Air Canada", "flightNumber": "AC 702", "from": "YYZ", "to": "LGA",
           "confirmation": "DEMO-NY1"},
          [_pt("from", "YYZ", (43.6777, -79.6248)), _pt("to", "LGA", (40.7769, -73.8740))]),
         ("lodging", (-62, "15:00"), (-58, "11:00"),
          {"name": "Midtown Hotel (sample)", "type": "Hotel",
           "address": "West 44th Street, Manhattan, New York", "confirmation": "DEMO-MH3"},
          [_pt("place", "Midtown Hotel", (40.7570, -73.9840))]),
         ("event", (-61, "19:30"), None,
          {"name": "A Broadway show", "venue": "Theatre District, Manhattan", "seat": "Mezz C 104"},
          [_pt("place", "Theatre District", (40.7590, -73.9866))]),
         ("dining", (-60, "12:30"), None,
          {"name": "Chelsea Market lunch", "address": "Chelsea, Manhattan, New York"},
          [_pt("place", "Chelsea Market", (40.7420, -74.0048))]),
         ("tour", (-60, "15:00"), (-60, "17:00"),
          {"name": "High Line walk", "location": "Manhattan, New York", "duration": "2 h"},
          [_pt("place", "The High Line", (40.7480, -74.0048))]),
         ("flight", (-58, "17:00"), (-58, "18:35"),
          {"airline": "Air Canada", "flightNumber": "AC 717", "from": "LGA", "to": "YYZ",
           "confirmation": "DEMO-NY1"},
          [_pt("from", "LGA", (40.7769, -73.8740)), _pt("to", "YYZ", (43.6777, -79.6248))]),
     ]},
]

# Discover content written as an AI agent would (topic, place_key, title, body,
# extra FindingIn fields). Real, long-standing places; no bookings.
_SRC = [{"title": "Visit Portugal", "url": "https://www.visitportugal.com"}]
RESEARCH = {
    "lisbon": [
        {"topic": "food", "place_key": "PT:Lisbon", "title": "Food & drink in Lisbon",
         "body_md": "Seafood, custard tarts and late dinners. Book ahead for Friday and "
                    "Saturday nights; tascas (small family places) rarely take bookings.",
         "sources": _SRC,
         "items": [
             {"kind": "bakery", "name": "Pastéis de Belém", "neighbourhood": "Belém",
              "what_to_get": "Pastéis de nata, warm, with cinnamon", "price_tier": 1,
              "lat": 38.6975, "lon": -9.2032},
             {"kind": "market", "name": "Time Out Market Lisboa", "neighbourhood": "Cais do Sodré",
              "what_to_get": "A bifana and a glass of vinho verde", "price_tier": 2,
              "lat": 38.7071, "lon": -9.1460},
             {"kind": "bar", "name": "A Ginjinha", "neighbourhood": "Rossio",
              "what_to_get": "A shot of ginjinha (sour-cherry liqueur)", "price_tier": 1,
              "lat": 38.7145, "lon": -9.1383}]},
        {"topic": "events", "place_key": "PT:Lisbon", "title": "Things to do in Lisbon",
         "body_md": "Hilly and walkable: ride tram 28 once, then walk. Most museums close "
                    "on Mondays.", "sources": _SRC,
         "items": [
             {"kind": "activity", "name": "Tram 28 through Alfama", "duration": "1 h",
              "details": "The classic yellow tram through the old town; go early to get a seat.",
              "lat": 38.7131, "lon": -9.1306},
             {"kind": "activity", "name": "Castelo de São Jorge", "duration": "2 h",
              "details": "Moorish castle with the best view over the city.", "booking": False,
              "lat": 38.7139, "lon": -9.1335},
             {"kind": "activity", "name": "LX Factory", "duration": "2 h",
              "details": "Old industrial site turned into shops, cafés and a bookshop.",
              "lat": 38.7037, "lon": -9.1784}]},
        {"topic": "food", "place_key": "PT:Porto", "title": "Food & drink in Porto",
         "body_md": "Port lodges across the river in Gaia, and the francesinha sandwich.",
         "sources": _SRC,
         "items": [
             {"kind": "market", "name": "Mercado do Bolhão", "neighbourhood": "Baixa",
              "what_to_get": "Fresh fruit and a sandes de presunto", "price_tier": 1,
              "lat": 41.1496, "lon": -8.6066},
             {"kind": "winery", "name": "Port lodges of Vila Nova de Gaia",
              "neighbourhood": "Gaia", "what_to_get": "A tawny tasting flight", "price_tier": 2,
              "lat": 41.1373, "lon": -8.6136}]},
        {"topic": "safety", "place_key": "PT", "title": "Safety & getting around in Portugal",
         "body_md": "", "sources": _SRC,
         "safety_transport": {
             "safety": {"summary": "Portugal is one of Europe's safest countries. Watch for "
                                   "pickpockets on tram 28 and around busy viewpoints.",
                        "advisories": [{"text": "Exercise normal security precautions.",
                                        "source_url": "https://travel.gc.ca/destinations/portugal"}],
                        "scams": ["People selling 'hashish' (usually bay leaves) in Baixa."]},
             "transport": {"summary": "Metro, trams and funiculars in Lisbon; trains between cities.",
                           "public_transit": "Load a Viva Viagem card for metro, trams and buses.",
                           "from_airport": "Metro red line from the airport to the centre in ~25 min.",
                           "ride_hailing": ["Uber", "Bolt"], "apps": ["CP (trains)"],
                           "tips": ["Wear shoes with grip: the calçada stones are slippery."]}}},
        {"topic": "language_local", "place_key": "PT:Lisbon", "title": "Local phrases in Lisbon",
         "body_md": "",
         "phrases": {"register_summary": "Polite and a little formal with strangers; "
                                         "a 'bom dia' goes a long way.",
                     "greetings": [{"phrase": "Bom dia", "meaning": "Good morning"},
                                   {"phrase": "Obrigado / Obrigada", "meaning": "Thank you",
                                    "note": "Obrigado if you're a man, obrigada if you're a woman"}],
                     "expressions": [{"phrase": "A conta, por favor", "meaning": "The bill, please"},
                                     {"phrase": "Uma bica", "meaning": "An espresso (Lisbon word)"}],
                     "etiquette": ["Tipping is small: round up or leave 5–10%."]}},
    ],
    "banff": [
        {"topic": "events", "place_key": "CA:Banff", "title": "Things to do around Banff",
         "body_md": "Start early: parking at Lake Louise and Moraine Lake fills by 8 a.m., "
                    "and the Moraine Lake road is shuttle-only.",
         "sources": [{"title": "Parks Canada", "url": "https://parks.canada.ca/pn-np/ab/banff"}],
         "items": [
             {"kind": "activity", "name": "Johnston Canyon catwalks", "duration": "2 h",
              "details": "An easy walk along catwalks to the Lower and Upper Falls.",
              "lat": 51.2453, "lon": -115.8395},
             {"kind": "activity", "name": "Banff Upper Hot Springs", "duration": "1.5 h",
              "details": "Hot pool with mountain views; good after a day of hiking.",
              "lat": 51.1525, "lon": -115.5612}]},
        {"topic": "food", "place_key": "CA:Banff", "title": "Food & drink in Banff",
         "body_md": "Hearty and casual. Book dinner on weekends.",
         "sources": [{"title": "Banff & Lake Louise Tourism", "url": "https://www.banfflakelouise.com"}],
         "items": [
             {"kind": "brewery", "name": "Banff Ave. Brewing Co.", "neighbourhood": "Downtown",
              "what_to_get": "A flight of local beers", "price_tier": 2,
              "lat": 51.1763, "lon": -115.5713}]},
        {"topic": "safety", "place_key": "CA", "title": "Safety in the Rockies", "body_md": "",
         "sources": [{"title": "Parks Canada", "url": "https://parks.canada.ca/pn-np/ab/banff"}],
         "safety_transport": {
             "safety": {"summary": "Wildlife is the main hazard: keep your distance from elk "
                                   "and bears, and carry bear spray on trails.",
                        "advisories": [{"text": "Check trail and wildlife warnings before hiking.",
                                        "source_url": "https://parks.canada.ca/pn-np/ab/banff"}],
                        "scams": []},
             "transport": {"summary": "A car makes it easy; Roam Transit buses link Banff, "
                                      "the gondola and Lake Louise.",
                           "tips": ["Buy a Parks Canada pass before you drive in."]}}},
    ],
}

JOURNAL = [   # (trip key, day offset, "HH:MM", body, place)
    ("nyc", -62, "21:40", "Landed, checked in, and walked to Times Square just to say we did. "
                          "Too loud, loved it anyway.", ("Times Square", 40.7580, -73.9855)),
    ("nyc", -61, "23:10", "The show was worth every cent. Sam cried at the second act.",
     ("Theatre District", 40.7590, -73.9866)),
    ("nyc", -60, "18:05", "High Line at golden hour, then dumplings. Feet are done.",
     ("The High Line", 40.7480, -74.0048)),
    ("lisbon", -1, "22:15", "First night in Lisbon. Pastéis de nata count: 3.",
     ("Bairro Alto", 38.7128, -9.1446)),
]

EXPENSES = [  # (trip key, day offset, title, amount, currency, rate→CAD, category, payer)
    # Split equally between everyone on the trip.
    ("nyc", -62, "Taxi from LGA", "48.00", "USD", "1.38", "transport", "alex"),
    ("nyc", -61, "Show tickets", "310.00", "USD", "1.38", "activities", "sam"),
    ("nyc", -60, "Chelsea Market lunch", "62.50", "USD", "1.38", "food", "alex"),
    ("nyc", -59, "Museum entry", "60.00", "USD", "1.38", "activities", "sam"),
    ("lisbon", -1, "Metro cards", "14.00", "EUR", "1.51", "transport", "alex"),
    ("lisbon", -1, "Dinner in Bairro Alto", "58.00", "EUR", "1.51", "food", "sam"),
]

BUDGET = [    # (trip key, title, amount, currency, category)
    ("nyc", "Hotel, four nights", "1450.00", "CAD", "lodging"),
    ("nyc", "Food and drink", "600.00", "CAD", "food"),
    ("nyc", "Shows and museums", "450.00", "CAD", "activities"),
    ("lisbon", "Hotels", "1100.00", "CAD", "lodging"),
    ("lisbon", "Food and drink", "500.00", "CAD", "food"),
]


# --------------------------------------------------------------------------- helpers
def _d(today: date, offset: int) -> str:
    return (today + timedelta(days=offset)).isoformat()


def _encode_polyline6(points) -> str:
    """Google's encoded polyline at precision 6 (what routes store)."""
    out, plat, plon = [], 0, 0
    for lat, lon in points:
        ilat, ilon = round(lat * 1e6), round(lon * 1e6)
        for v in (ilat - plat, ilon - plon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1f)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(out)


# Calgary Airport → Banff, a handful of points along the Trans-Canada.
_BANFF_ROUTE = [(51.1215, -114.0076), (51.0870, -114.2100), (51.0900, -114.6200),
                (51.0870, -115.0500), (51.0870, -115.3500), (51.1784, -115.5708)]


def _geo(atype: str, values: dict, points: list[dict]) -> dict:
    from app.trips.geo import fingerprint
    g = {"points": points, "key": fingerprint(atype, values), "complete": True,
         "at": datetime.now(timezone.utc).isoformat()}
    if atype == "drive":
        line = _BANFF_ROUTE if values.get("to") == "Banff" else list(reversed(_BANFF_ROUTE))
        g["route"] = {"polyline": _encode_polyline6(line), "distance_km": 145.0,
                      "duration_minutes": 100, "provider": "capture"}
    return g


# Alex is the demo login's own traveller; Sam is a guest with no login.
_PEOPLE = (("alex", "Alex Morgan", "self"), ("sam", "Sam Rivera", "guest"))
_PEOPLE_NAMES = [name for _, name, _ in _PEOPLE]


def _demo_user(session: Session):
    from app.models import User
    return session.query(User).filter_by(username=settings.demo_username.strip()).one()


def _travellers(session: Session, user) -> dict:
    """Alex (the demo login's own traveller) and Sam (a guest with no login)."""
    from app.models import TravelerIdentity
    out = {}
    for key, name, kind in _PEOPLE:
        t = session.query(TravelerIdentity).filter_by(display_name=name).one_or_none()
        if t is None:
            t = TravelerIdentity(display_name=name, kind=kind, nationality="CA",
                                 base_currency="CAD", timezone="America/Toronto")
            session.add(t)
            session.flush()
        out[key] = t.id
    user.traveler_id = out["alex"]
    # Home is Toronto, so Discover doesn't research it and the trips from YYZ
    # aren't visits (as on a real account).
    from app.models import HomeProfile
    if not session.query(HomeProfile).filter_by(traveler_id=out["alex"]).count():
        session.add_all([
            HomeProfile(traveler_id=out["alex"], kind="residence", label="Toronto",
                        lat=43.6532, lon=-79.3832, radius_km=50.0, country="CA"),
            HomeProfile(traveler_id=out["alex"], kind="home_airport", label="Toronto Pearson",
                        iata="YYZ", lat=43.6777, lon=-79.6248, radius_km=50.0, country="CA")])
    session.commit()
    return out


def seeded_trips(session: Session):
    from app.models import Trip
    return session.query(Trip).filter(Trip.external_ref.like(TAG + "%")).all()


# --------------------------------------------------------------------------- seed
def seed(session: Session, *, replace: bool = False, today: date | None = None) -> dict:
    """Write the sample data for the demo user. Already seeded: nothing, unless
    `replace` (a release changed the seed), which removes the seeded trips and
    everything on them first."""
    from app import demo
    today = today or demo_today()
    user = _demo_user(session)
    if seeded_trips(session):
        if not replace:
            return {"seeded": False, "reason": "already seeded (use --replace)"}
        _remove(session, user)
    people = _travellers(session, user)
    with demo.seeding():
        trips = {t["key"]: _seed_trip(session, user, people, t, today) for t in TRIPS}
        _seed_discover(session, user, trips)
        _seed_packing(session, user, trips["banff"])
        _seed_costs(session, user, people, trips, today)
        _seed_journal(session, user, trips, today)
        _seed_captures(session, user, today)
    log.info("demo seed written: %s", ", ".join(t.name for t in trips.values()))
    return {"seeded": True, "trips": [t.name for t in trips.values()]}


def _seed_trip(session, user, people, spec, today):
    from app.models import Trip, TripActivity, TripTraveler
    t = Trip(name=spec["name"], source="manual", owner_id=user.id, notes=spec["notes"],
             start_date=_d(today, spec["start"]), end_date=_d(today, spec["end"]),
             external_ref=TAG + spec["key"], expense_currency=spec["currency"])
    session.add(t)
    session.flush()
    session.add(TripTraveler(trip_id=t.id, traveler_id=people["alex"], role="self"))
    if spec["companion"]:
        session.add(TripTraveler(trip_id=t.id, traveler_id=people["sam"], role="companion"))
    for atype, (sd, st), end, values, points in spec["activities"]:
        v = dict(values, when=f"{_d(today, sd)}T{st}")
        if end:
            v["endWhen"] = f"{_d(today, end[0])}T{end[1]}"
        session.add(TripActivity(trip_id=t.id, type=atype, starts_at=v["when"], values=v,
                                 geo=_geo(atype, v, points)))
    session.commit()
    return t


def _seed_discover(session, user, trips):
    """The deterministic topics (plugs, currency, weather, language) through the
    real runner, offline; then the agent-written topics."""
    from app import api_v1
    refresh_discover(session, user, trips.values())
    for key, findings in RESEARCH.items():
        for f in findings:
            api_v1.save_research_finding(session, user, trips[key].id, api_v1.FindingIn(**f),
                                         agent="Run·dere demo")


def refresh_discover(session, user, trips) -> None:
    from app.resolving import load_home_profiles
    from app.trips.routes import _airport_resolver, _resolvers
    from discover.runner import run_discovery
    homes = load_home_profiles(session, user.traveler_id)
    with offline_feeds():
        for t in trips:
            run_discovery(session, t, resolve_airport=_airport_resolver(),
                          geocoder=SeedGeocoder(), resolvers=_resolvers(),
                          home_profiles=homes, base_currencies=("CAD",), user_id=user.id)


def _seed_packing(session, user, trip):
    from app.trips.routes import _airport_resolver
    from planning.runner import generate_plan
    try:
        with offline_feeds():
            from app.resolving import load_home_profiles
            generate_plan(session, trip, resolve_airport=_airport_resolver(),
                          geocoder=SeedGeocoder(), user_id=user.id,
                          home_profiles=load_home_profiles(session, user.traveler_id),
                          traveler_id=user.traveler_id)
    except Exception:                       # pragma: no cover - a showcase extra
        log.exception("demo packing plan failed; the trip has no packing list")


def _seed_costs(session, user, people, trips, today):
    from app import api_v1
    for key, title, amount, cur, cat in BUDGET:
        tid = trips[key].id
        api_v1.api_add_budget_item(tid, api_v1.BudgetItemIn(
            title=title, amount=amount, currency=cur, category=cat,
            traveler_ids=_roster_ids(session, tid)), user=user, session=session)
    for key, day, title, amount, cur, rate, cat, payer in EXPENSES:
        api_v1.api_add_expense(trips[key].id, api_v1.ExpenseIn(
            amount=amount, currency=cur, rate=rate, payer_id=people[payer], title=title,
            spent_on=_d(today, day), category=cat, source="api"), user=user, session=session)


def _roster_ids(session, trip_id) -> list[int]:
    from app.models import TripTraveler
    return [r.traveler_id for r in session.query(TripTraveler).filter_by(trip_id=trip_id)]


def _seed_journal(session, user, trips, today):
    from app import api_v1
    for key, day, hm, body, (label, lat, lon) in JOURNAL:
        when = _d(today, day)
        api_v1.api_journal_create(api_v1.JournalCreate(
            entry_date=when, captured_at=f"{when}T{hm}:00", body=body, trip_id=trips[key].id,
            place_lat=lat, place_lon=lon, place_label=label, source="mobile"),
            user=user, session=session)


def _ld_page(doc: dict) -> str:
    return (f'<html><head><script type="application/ld+json">{json.dumps(doc)}</script>'
            "</head><body>Your booking (Run·dere demo sample).</body></html>")


def _seed_captures(session, user, today):
    """Two bookings for a trip that isn't planned yet, through the real capture
    pipeline (deterministic JSON-LD, no AI), left waiting in Review."""
    from app.capture import _queue_web
    from app.models import IngestBatch
    from app.processing import process_batch
    dep = today + timedelta(days=90)
    docs = [
        {"@context": "https://schema.org", "@type": "FlightReservation",
         "reservationNumber": "DEMO-TK9", "underName": {"@type": "Person", "name": "Alex Morgan"},
         "reservationFor": {"@type": "Flight", "flightNumber": "AC1",
                            "airline": {"@type": "Airline", "name": "Air Canada", "iataCode": "AC"},
                            "departureAirport": {"@type": "Airport", "iataCode": "YYZ"},
                            "departureTime": f"{dep.isoformat()}T13:10:00-05:00",
                            "arrivalAirport": {"@type": "Airport", "iataCode": "HND"},
                            "arrivalTime": f"{(dep + timedelta(days=1)).isoformat()}T15:25:00+09:00"}},
        {"@context": "https://schema.org", "@type": "LodgingReservation",
         "reservationNumber": "DEMO-TH4", "underName": {"@type": "Person", "name": "Alex Morgan"},
         "reservationFor": {"@type": "LodgingBusiness", "name": "Shinjuku Hotel (sample)",
                            "address": {"@type": "PostalAddress", "addressLocality": "Tokyo",
                                        "addressCountry": "JP"}},
         "checkinTime": f"{(dep + timedelta(days=1)).isoformat()}T16:00:00+09:00",
         "checkoutTime": f"{(dep + timedelta(days=6)).isoformat()}T11:00:00+09:00"},
    ]
    for doc in docs:
        bid = _queue_web(session, _ld_page(doc), source_url=None, submitted_by="demo",
                         owner_user_id=user.id, title="Sample booking")
        process_batch(session, session.get(IngestBatch, bid), None)


def _remove(session, user) -> None:
    """Remove the seeded trips and everything that belongs to them, plus the demo
    user's captures (a re-seed starts clean)."""
    from app import models as m
    tids = [t.id for t in seeded_trips(session)]
    for model in (m.TripActivity, m.TripTraveler, m.DiscoveryFinding, m.DiscoveryItem,
                  m.DiscoveryRun, m.PackingPlan, m.PackingItem, m.TripExpense,
                  m.TripBudgetItem, m.TripRate):
        q = session.query(model).filter(model.trip_id.in_(tids))
        if model is m.TripExpense:
            ids = [e.id for e in q]
            session.query(m.TripExpenseShare).filter(
                m.TripExpenseShare.expense_id.in_(ids)).delete(synchronize_session=False)
        if model is m.TripBudgetItem:
            ids = [b.id for b in q]
            session.query(m.TripBudgetTraveler).filter(
                m.TripBudgetTraveler.budget_item_id.in_(ids)).delete(synchronize_session=False)
        q.delete(synchronize_session=False)
    session.query(m.JournalEntry).filter(m.JournalEntry.owner_id == user.id).delete(
        synchronize_session=False)
    _remove_captures(session, user)
    session.query(m.Trip).filter(m.Trip.id.in_(tids)).delete(synchronize_session=False)
    # The seed's people and Alex's homes (a re-seed recreates them).
    people = [t.id for t in session.query(m.TravelerIdentity).filter(
        m.TravelerIdentity.display_name.in_(_PEOPLE_NAMES))]
    session.query(m.HomeProfile).filter(m.HomeProfile.traveler_id.in_(people)).delete(
        synchronize_session=False)
    user.traveler_id = None
    session.query(m.TravelerIdentity).filter(m.TravelerIdentity.id.in_(people)).delete(
        synchronize_session=False)
    session.commit()


def _remove_captures(session, user) -> None:
    from app import models as m
    bids = [b.id for b in session.query(m.IngestBatch).filter(m.IngestBatch.user_id == user.id)]
    session.query(m.TravelItem).filter(m.TravelItem.batch_id.in_(bids)).delete(
        synchronize_session=False)
    session.query(m.IngestBatch).filter(m.IngestBatch.id.in_(bids)).delete(
        synchronize_session=False)


# --------------------------------------------------------------------------- roll
def demo_today() -> date:
    """Today on the demo server's clock (TX_DEMO_TIMEZONE)."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(settings.demo_timezone)).date()


def drift(session: Session, today: date | None = None) -> int | None:
    """How many days the seed must move to be current again: 0 = up to date,
    None = nothing seeded."""
    from app.models import Trip
    today = today or demo_today()
    anchor = session.query(Trip).filter_by(external_ref=ANCHOR_TRIP).one_or_none()
    if anchor is None or not anchor.start_date:
        return None
    return (today + timedelta(days=ANCHOR_OFFSET) - date.fromisoformat(anchor.start_date)).days


def _shift(value: str | None, days: int) -> str | None:
    """Move an ISO date or local date-time string by `days`, keeping its time."""
    if not value or len(value) < 10:
        return value
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return value
    return (d + timedelta(days=days)).isoformat() + value[10:]


def roll(session: Session, today: date | None = None) -> dict:
    """Shift every seeded date so the trips stay where the seed puts them
    (D-198). Idempotent: a second run the same day finds no drift."""
    from app import demo
    from app import models as m
    days = drift(session, today)
    if not days:
        return {"rolled": 0}
    user = _demo_user(session)
    trips = seeded_trips(session)
    tids = [t.id for t in trips]
    for t in trips:
        t.start_date, t.end_date = _shift(t.start_date, days), _shift(t.end_date, days)
    for a in session.query(m.TripActivity).filter(m.TripActivity.trip_id.in_(tids)):
        v = dict(a.values or {})
        for k in ("when", "endWhen"):
            if v.get(k):
                v[k] = _shift(v[k], days)
        a.values, a.starts_at = v, _shift(a.starts_at, days)
    for i in session.query(m.DiscoveryItem).filter(m.DiscoveryItem.trip_id.in_(tids)):
        i.starts_on, i.ends_on = _shift(i.starts_on, days), _shift(i.ends_on, days)
    for e in session.query(m.TripExpense).filter(m.TripExpense.trip_id.in_(tids)):
        e.spent_on, e.rate_date = _shift(e.spent_on, days), _shift(e.rate_date, days)
    for b in session.query(m.TripBudgetItem).filter(m.TripBudgetItem.trip_id.in_(tids)):
        b.start_date, b.end_date = _shift(b.start_date, days), _shift(b.end_date, days)
        b.rate_date = _shift(b.rate_date, days)
    for r in session.query(m.TripRate).filter(m.TripRate.trip_id.in_(tids)):
        r.rate_date, r.as_of = _shift(r.rate_date, days), _shift(r.as_of, days)
    for j in session.query(m.JournalEntry).filter(m.JournalEntry.owner_id == user.id):
        j.entry_date = _shift(j.entry_date, days)
        if j.captured_at:
            j.captured_at = j.captured_at + timedelta(days=days)
    session.commit()
    # The date-driven findings and the waiting captures are rebuilt for the new
    # dates; everything else only moved.
    with demo.seeding():
        refresh_discover(session, user, trips)
        _remove_captures(session, user)
        session.commit()
        _seed_captures(session, user, today or demo_today())
    log.info("demo roll: moved the sample trips %+d day(s)", days)
    return {"rolled": days}


# --------------------------------------------------------------------------- schedule
_stop = threading.Event()
_thread: threading.Thread | None = None


def _seconds_until_3am() -> float:
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(settings.demo_timezone))
    nxt = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def _run_once(session_factory) -> None:
    try:
        with session_factory() as session:
            if not seeded_trips(session):
                seed(session)
            else:
                roll(session)
    except Exception:                       # pragma: no cover - never kill the thread
        log.exception("demo roll failed; trying again at the next run")


def _loop(session_factory) -> None:
    _run_once(session_factory)              # at startup: catch up after downtime
    while not _stop.wait(_seconds_until_3am()):
        _run_once(session_factory)


def start(session_factory) -> bool:
    """Seed if needed, roll now, then roll daily at 03:00 (once per process)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(session_factory,), name="demo-roll",
                               daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()
