"""The demo server's sample data and its nightly roll-forward (YONDER-181, D-198).

`seed()` writes three fictional trips for the demo user, in the shapes the app
itself writes. Each is dated relative to today, so there's always one trip under
way, one coming up and one in the past:

- **Lisbon & Porto**, under way (day 2 of 5): a timeline with moments in the
  next 48 hours (Today, the Watch, the Live Activity), the full Discover set,
  costs in euros.
- **Paris to Nice road trip**, about three weeks out: a drive with a route
  line, Discover and a generated packing plan.
- **New York City**, about two months ago: journal entries, costs split with a
  companion, and a plan against actuals.

The demo user's traveller is Jane Doe, who lives in central London and flies
from Heathrow (British Airways, and a Ryanair hop home into Stansted). Her home
profile is what keeps London out of Discover. Plus two captures waiting in
Review, for a trip that isn't planned yet.

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
    "london": ("GB", "London", 51.5074, -0.1278),
    "lisbon": ("PT", "Lisbon", 38.7223, -9.1393),
    "porto": ("PT", "Porto", 41.1579, -8.6291),
    "paris": ("FR", "Paris", 48.8566, 2.3522),
    "nice": ("FR", "Nice", 43.7102, 7.2620),
    "eze": ("FR", "Èze", 43.7280, 7.3615),
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
_RATES = {("GBP", "EUR"): 1.17, ("EUR", "GBP"): 0.85, ("GBP", "USD"): 1.27,
          ("USD", "GBP"): 0.79, ("EUR", "USD"): 1.09, ("USD", "EUR"): 0.92,
          ("GBP", "JPY"): 190.0, ("JPY", "GBP"): 0.0053}
# Rough daily highs/lows (°C), rain (mm/day), humidity (%) and elevation (m) by
# city. Approximations for a showcase, not records.
_CLIMATE = {
    "London": (18, 10, 1.7, 75, 15), "Lisbon": (24, 15, 1.2, 65, 50),
    "Porto": (21, 13, 2.5, 72, 90), "Paris": (20, 12, 1.8, 70, 35),
    "Nice": (25, 18, 0.8, 65, 10), "Èze": (24, 17, 0.8, 62, 430),
    "New York": (20, 12, 3.0, 64, 10),
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
# Jane flies from Heathrow (her home airport); times are local wall clock.
LHR = (51.4700, -0.4543)
TRIPS = [
    {"key": "lisbon", "name": "Lisbon & Porto", "start": -1, "end": 3,
     "notes": "Sample trip on the Run·dere demo server. Five days: Lisbon, then the "
              "train north to Porto, home on a Ryanair hop into Stansted.",
     "companion": True, "currency": "GBP",
     "activities": [
         ("flight", (-1, "07:05"), (-1, "09:45"),
          {"airline": "British Airways", "flightNumber": "BA 500", "from": "LHR", "to": "LIS",
           "confirmation": "DEMO-LX4"},
          [_pt("from", "LHR", LHR), _pt("to", "LIS", (38.7742, -9.1342))]),
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
         # Ryanair doesn't serve Heathrow; Stansted is in London's metro group, so
         # this still counts as coming home (resolve/home.py METRO_GROUPS).
         ("flight", (3, "13:40"), (3, "16:15"),
          {"airline": "Ryanair", "flightNumber": "FR 8337", "from": "OPO", "to": "STN",
           "confirmation": "DEMO-FR2"},
          [_pt("from", "OPO", (41.2481, -8.6814)), _pt("to", "STN", (51.8860, 0.2389))]),
     ]},
    {"key": "france", "name": "Paris to Nice road trip", "start": 21, "end": 26,
     "notes": "Sample trip: two nights in Paris, then the drive south to the Riviera.",
     "companion": False, "currency": "GBP",
     "activities": [
         ("flight", (21, "08:15"), (21, "10:30"),
          {"airline": "British Airways", "flightNumber": "BA 304", "from": "LHR", "to": "CDG",
           "confirmation": "DEMO-PN1"},
          [_pt("from", "LHR", LHR), _pt("to", "CDG", (49.0097, 2.5479))]),
         ("lodging", (21, "15:00"), (23, "10:00"),
          {"name": "Hôtel du Marais (sample)", "type": "Hotel",
           "address": "Le Marais, Paris", "confirmation": "DEMO-HM6"},
          [_pt("place", "Hôtel du Marais", (48.8590, 2.3620))]),
         ("tour", (22, "10:00"), (22, "13:00"),
          {"name": "Musée d'Orsay", "location": "Paris", "duration": "3 h"},
          [_pt("place", "Musée d'Orsay", (48.8600, 2.3266))]),
         ("carRental", (23, "09:00"), (26, "10:00"),
          {"company": "Sample Car Rental", "pickupLocation": "Paris Gare de Lyon",
           "dropoffLocation": "Nice Côte d'Azur Airport (NCE)", "confirmation": "DEMO-CR5"},
          [_pt("from", "Paris Gare de Lyon", (48.8443, 2.3743)),
           _pt("to", "NCE", (43.6584, 7.2159))]),
         ("drive", (23, "09:30"), (23, "18:30"),
          {"route": "Paris → Nice (A6 / A7 / A8)", "from": "Paris", "to": "Nice",
           "distance": "930 km",
           # The saved directions link, so Map opens the route itself rather than a
           # search for the route's text (owner, 2026-10-14).
           "mapLink": "https://maps.app.goo.gl/SWTsFzrYAUioGcgJ7"},
          [_pt("from", "Paris", "paris"), _pt("to", "Nice", "nice")]),
         ("lodging", (23, "19:00"), (26, "09:00"),
          {"name": "Villa Promenade (sample)", "type": "Hotel",
           "address": "Promenade des Anglais, Nice", "confirmation": "DEMO-VP8"},
          [_pt("place", "Villa Promenade", (43.6950, 7.2650))]),
         ("tour", (24, "10:00"), (24, "15:00"),
          {"name": "Èze village and the Grande Corniche", "location": "Èze, France",
           "duration": "5 h"},
          [_pt("place", "Èze", "eze")]),
         ("flight", (26, "12:50"), (26, "14:00"),
          {"airline": "British Airways", "flightNumber": "BA 343", "from": "NCE", "to": "LHR",
           "confirmation": "DEMO-PN1"},
          [_pt("from", "NCE", (43.6584, 7.2159)), _pt("to", "LHR", LHR)]),
     ]},
    {"key": "nyc", "name": "New York City", "start": -62, "end": -58,
     "notes": "Sample trip from two months ago: a long weekend in Manhattan.",
     "companion": True, "currency": "GBP",
     "activities": [
         ("flight", (-62, "08:25"), (-62, "11:20"),
          {"airline": "British Airways", "flightNumber": "BA 117", "from": "LHR", "to": "JFK",
           "confirmation": "DEMO-NY1"},
          [_pt("from", "LHR", LHR), _pt("to", "JFK", (40.6413, -73.7781))]),
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
         ("flight", (-58, "18:30"), (-57, "06:35"),
          {"airline": "British Airways", "flightNumber": "BA 112", "from": "JFK", "to": "LHR",
           "confirmation": "DEMO-NY1"},
          [_pt("from", "JFK", (40.6413, -73.7781)), _pt("to", "LHR", LHR)]),
     ]},
]

# Discover content written as an AI agent would (topic, place_key, title, body,
# extra FindingIn fields). Real, long-standing places; no bookings. Safety
# advice cites the UK government, as Jane is British.
_SRC_PT = [{"title": "Visit Portugal", "url": "https://www.visitportugal.com"}]
_SRC_FR = [{"title": "France.fr", "url": "https://www.france.fr"}]
RESEARCH = {
    "lisbon": [
        {"topic": "food", "place_key": "PT:Lisbon", "title": "Food & drink in Lisbon",
         "body_md": "Seafood, custard tarts and late dinners. Book ahead for Friday and "
                    "Saturday nights; tascas (small family places) rarely take bookings.",
         "sources": _SRC_PT,
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
                    "on Mondays.", "sources": _SRC_PT,
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
         "sources": _SRC_PT,
         "items": [
             {"kind": "market", "name": "Mercado do Bolhão", "neighbourhood": "Baixa",
              "what_to_get": "Fresh fruit and a sandes de presunto", "price_tier": 1,
              "lat": 41.1496, "lon": -8.6066},
             {"kind": "winery", "name": "Port lodges of Vila Nova de Gaia",
              "neighbourhood": "Gaia", "what_to_get": "A tawny tasting flight", "price_tier": 2,
              "lat": 41.1373, "lon": -8.6136}]},
        {"topic": "safety", "place_key": "PT", "title": "Safety & getting around in Portugal",
         "body_md": "", "sources": _SRC_PT,
         "safety_transport": {
             "safety": {"summary": "Portugal is one of Europe's safest countries. Watch for "
                                   "pickpockets on tram 28 and around busy viewpoints.",
                        "advisories": [{"text": "Most visits are trouble-free; watch for pickpockets "
                                                "in tourist areas and on public transport.",
                                        "source_url": "https://www.gov.uk/foreign-travel-advice/portugal"}],
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
    "france": [
        {"topic": "food", "place_key": "FR:Nice", "title": "Food & drink in Nice",
         "body_md": "Niçois cooking leans Italian: socca, pissaladière and salade niçoise. "
                    "Lunch is the main meal; many kitchens close between 14:30 and 19:00.",
         "sources": _SRC_FR,
         "items": [
             {"kind": "street_food", "name": "Chez Pipo", "neighbourhood": "Port",
              "what_to_get": "Socca, straight from the wood oven", "price_tier": 1,
              "lat": 43.7010, "lon": 7.2860},
             {"kind": "market", "name": "Cours Saleya market", "neighbourhood": "Vieux Nice",
              "what_to_get": "Morning flowers and produce; socca from the stalls", "price_tier": 1,
              "lat": 43.6955, "lon": 7.2757}]},
        {"topic": "events", "place_key": "FR:Nice", "title": "Things to do around Nice",
         "body_md": "Walk the Promenade at sunset; the hill villages are an easy drive east.",
         "sources": _SRC_FR,
         "items": [
             {"kind": "activity", "name": "Colline du Château", "duration": "1.5 h",
              "details": "Climb (or take the lift) for the view over the Baie des Anges.",
              "lat": 43.6950, "lon": 7.2810},
             {"kind": "activity", "name": "Musée Matisse", "duration": "2 h",
              "details": "Matisse's work in a Genoese villa in Cimiez.",
              "lat": 43.7196, "lon": 7.2760}]},
        {"topic": "events", "place_key": "FR:Paris", "title": "Things to do in Paris",
         "body_md": "Book big museums ahead; many close one weekday (the Louvre on Tuesdays).",
         "sources": _SRC_FR,
         "items": [
             {"kind": "activity", "name": "Sainte-Chapelle", "duration": "1 h",
              "details": "Thirteenth-century stained glass; go on a sunny morning.",
              "booking": True, "lat": 48.8554, "lon": 2.3450},
             {"kind": "activity", "name": "Canal Saint-Martin walk", "duration": "1.5 h",
              "details": "Iron footbridges, cafés and the locks.",
              "lat": 48.8710, "lon": 2.3650}]},
        {"topic": "safety", "place_key": "FR", "title": "Safety & driving in France", "body_md": "",
         "sources": _SRC_FR,
         "safety_transport": {
             "safety": {"summary": "Generally safe. Pickpocketing is common in central Paris "
                                   "and on the Riviera's busy promenades.",
                        "advisories": [{"text": "Carry your driving licence, insurance and V5C "
                                                "if you drive your own car.",
                                        "source_url": "https://www.gov.uk/foreign-travel-advice/france"}],
                        "scams": ["The 'found gold ring' and petition-signing tricks near landmarks."]},
             "transport": {"summary": "Métro in Paris; motorways south are tolled (péage).",
                           "apps": ["Bonjour RATP", "SNCF Connect"],
                           "tips": ["Tolls from Paris to Nice run to about €80; keep a card handy.",
                                    "Speed cameras are frequent: 130 km/h on motorways, 110 in rain."]}}},
        {"topic": "language_local", "place_key": "FR:Nice", "title": "Local phrases in Nice",
         "body_md": "",
         "phrases": {"register_summary": "Always start with 'bonjour' before asking anything; "
                                         "it's expected, and service warms up when you do.",
                     "greetings": [{"phrase": "Bonjour", "meaning": "Hello / good day"},
                                   {"phrase": "Bonsoir", "meaning": "Good evening"}],
                     "expressions": [{"phrase": "L'addition, s'il vous plaît", "meaning": "The bill, please"},
                                     {"phrase": "Une socca, s'il vous plaît",
                                      "meaning": "One socca, please"}],
                     "etiquette": ["Service is included; round up for good service."]}},
    ],
}

JOURNAL = [   # (trip key, day offset, "HH:MM", body, place)
    ("nyc", -62, "21:40", "Landed, checked in, and walked to Times Square just to say we did. "
                          "Too loud, loved it anyway.", ("Times Square", 40.7580, -73.9855)),
    ("nyc", -61, "23:10", "The show was worth every penny. Sam cried at the second act.",
     ("Theatre District", 40.7590, -73.9866)),
    ("nyc", -60, "18:05", "High Line at golden hour, then dumplings. Feet are done.",
     ("The High Line", 40.7480, -74.0048)),
    ("lisbon", -1, "22:15", "First night in Lisbon. Pastéis de nata count: 3.",
     ("Bairro Alto", 38.7128, -9.1446)),
]

EXPENSES = [  # (trip key, day offset, title, amount, currency, rate→GBP, category, payer)
    # Split equally between everyone on the trip.
    ("nyc", -62, "Taxi from JFK", "70.00", "USD", "0.79", "transport", "jane"),
    ("nyc", -61, "Show tickets", "310.00", "USD", "0.79", "activities", "sam"),
    ("nyc", -60, "Chelsea Market lunch", "62.50", "USD", "0.79", "food", "jane"),
    ("nyc", -59, "Museum entry", "60.00", "USD", "0.79", "activities", "sam"),
    ("lisbon", -1, "Metro cards", "14.00", "EUR", "0.85", "transport", "jane"),
    ("lisbon", -1, "Dinner in Bairro Alto", "58.00", "EUR", "0.85", "food", "sam"),
]

BUDGET = [    # (trip key, title, amount, currency, category)
    ("nyc", "Hotel, four nights", "950.00", "GBP", "lodging"),
    ("nyc", "Food and drink", "400.00", "GBP", "food"),
    ("nyc", "Shows and museums", "300.00", "GBP", "activities"),
    ("lisbon", "Hotels", "650.00", "GBP", "lodging"),
    ("lisbon", "Food and drink", "300.00", "GBP", "food"),
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


# Paris → Nice down the A6 / A7 / A8, a handful of points along the way.
_ROAD_ROUTE = [(48.8566, 2.3522), (47.7986, 3.5673), (47.0260, 4.8400), (45.7640, 4.8357),
               (44.9334, 4.8924), (43.5297, 5.4474), (43.4332, 6.7370), (43.7102, 7.2620)]


def _geo(atype: str, values: dict, points: list[dict]) -> dict:
    from app.trips.geo import fingerprint
    g = {"points": points, "key": fingerprint(atype, values), "complete": True,
         "at": datetime.now(timezone.utc).isoformat()}
    if atype == "drive":
        g["route"] = {"polyline": _encode_polyline6(_ROAD_ROUTE), "distance_km": 930.0,
                      "duration_minutes": 540, "provider": "capture"}
    return g


# Jane is the demo login's own traveller; Sam is a guest with no login.
_PEOPLE = (("jane", "Jane Doe", "self"), ("sam", "Sam Rivera", "guest"))
_PEOPLE_NAMES = [name for _, name, _ in _PEOPLE]


def _demo_user(session: Session):
    from app.models import User
    return session.query(User).filter_by(username=settings.demo_username.strip()).one()


def _travellers(session: Session, user) -> dict:
    """Jane (the demo login's own traveller) and Sam (a guest with no login),
    both British and based in London."""
    from app.models import TravelerIdentity
    out = {}
    for key, name, kind in _PEOPLE:
        t = session.query(TravelerIdentity).filter_by(display_name=name).one_or_none()
        if t is None:
            t = TravelerIdentity(display_name=name, kind=kind, nationality="GB",
                                 base_currency="GBP", timezone="Europe/London")
            session.add(t)
            session.flush()
        out[key] = t.id
    user.traveler_id = out["jane"]
    # The home profile the demo shows off: Jane lives within 10 km of central
    # London and flies from Heathrow. So Discover never researches London, and
    # flights home (Heathrow, or Stansted through London's metro group) aren't
    # visits, as on a real account.
    from app.models import HomeProfile
    if not session.query(HomeProfile).filter_by(traveler_id=out["jane"]).count():
        session.add_all([
            HomeProfile(traveler_id=out["jane"], kind="residence", label="Central London",
                        lat=51.5074, lon=-0.1278, radius_km=10.0, country="GB"),
            HomeProfile(traveler_id=out["jane"], kind="home_airport", label="London Heathrow",
                        iata="LHR", lat=51.4700, lon=-0.4543, radius_km=50.0, country="GB")])
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
        _seed_packing(session, user, trips["france"])
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
    session.add(TripTraveler(trip_id=t.id, traveler_id=people["jane"], role="self"))
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
                          home_profiles=homes, base_currencies=("GBP",), user_id=user.id)


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
         "reservationNumber": "DEMO-TK9", "underName": {"@type": "Person", "name": "Jane Doe"},
         "reservationFor": {"@type": "Flight", "flightNumber": "BA5",
                            "airline": {"@type": "Airline", "name": "British Airways",
                                        "iataCode": "BA"},
                            "departureAirport": {"@type": "Airport", "iataCode": "LHR"},
                            "departureTime": f"{dep.isoformat()}T19:15:00+01:00",
                            "arrivalAirport": {"@type": "Airport", "iataCode": "HND"},
                            "arrivalTime": f"{(dep + timedelta(days=1)).isoformat()}T17:20:00+09:00"}},
        {"@context": "https://schema.org", "@type": "LodgingReservation",
         "reservationNumber": "DEMO-TH4", "underName": {"@type": "Person", "name": "Jane Doe"},
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
    # The seed's people and Jane's homes (a re-seed recreates them).
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
