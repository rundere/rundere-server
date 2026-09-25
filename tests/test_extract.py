"""Extractor chain tests — synthetic fixtures only (D-21: no real emails here).

Covers: JSON-LD mapping, iCal parsing (timed + all-day), maps_url (happy path
+ RULE-31 date refusal), PDF text extraction, LLM retry logic (mocked), and
pipeline dispatch order. Every emitted batch is validated against the derived
extraction schema — emitters that drift from the contract fail here first.

Runnable under pytest or directly: python3 tests/test_extract.py
"""
import base64
import json
import sys
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extract import apple_maps_url, ical, jsonld, maps_url, mime, pipeline
from extract.llm import ExtractionFailed, OllamaExtractor, inline_refs, load_extraction_schema
from validation import validate_schema

SCHEMA = load_extraction_schema()


def _assert_valid(items):
    errors = validate_schema({"items": items, "declared_segment_count": None}, SCHEMA)
    assert not errors, errors[:5]


def _email_b64(html=None, text=None, ical_text=None, pdf=None, subject="Test"):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Test Sender <noreply@example.com>"
    msg["Date"] = "Mon, 3 Aug 2026 12:00:00 -0400"
    msg.set_content(text or "plain body")
    if html:
        msg.add_alternative(html, subtype="html")
    if ical_text:
        msg.add_attachment(ical_text.encode(), maintype="text", subtype="calendar")
    if pdf:
        msg.add_attachment(pdf, maintype="application", subtype="pdf",
                           filename="ticket.pdf")
    return base64.b64encode(msg.as_bytes()).decode()


# ---------------------------------------------------------------- jsonld

FLIGHT_LD = {
    "@context": "http://schema.org", "@type": "FlightReservation",
    "reservationNumber": "T4WNX8",
    "reservationStatus": "http://schema.org/ReservationConfirmed",
    "underName": {"@type": "Person", "name": "Pat Example"},
    "reservationFor": {
        "@type": "Flight", "flightNumber": "AC652",
        "airline": {"@type": "Airline", "name": "Air Canada", "iataCode": "AC"},
        "departureAirport": {"@type": "Airport", "name": "Toronto Pearson", "iataCode": "YYZ"},
        "departureTime": "2026-09-14T18:30:00-04:00",
        "arrivalAirport": {"@type": "Airport", "name": "Heathrow", "iataCode": "LHR"},
        "arrivalTime": "2026-09-15T06:40:00+01:00",
    },
}


def _ld_html(doc):
    return (f"<html><head><script type=\"application/ld+json\">"
            f"{json.dumps(doc)}</script></head><body>Your booking.</body></html>")


def test_jsonld_flight():
    items = jsonld.extract_jsonld(_ld_html(FLIGHT_LD))
    assert len(items) == 1
    _assert_valid(items)
    p = items[0]
    assert p["item_type"] == "flight"
    assert p["booking_reference"] == "T4WNX8"
    f = p["flight"]
    assert f["flight_number"] == "888"          # carrier prefix stripped
    assert f["departure"]["place"]["iata"] == "YYZ"
    assert f["departure"]["time"] == {"local": "2026-09-14T18:30",
                                      "precision": "datetime", "tz": None,
                                      "origin": "document"}   # offset dropped, never guessed
    assert p["travelers"][0]["raw_name"] == "Pat Example"


def test_grammar_forces_ticket_numbers_on_flight():
    """D-29 recurrence (2026-08-05): ticket_numbers was optional, so the
    grammar-constrained model never emitted it and e-ticket numbers (an AirTrail
    custom field) went uncaptured. It must be required in the flight variant."""
    from extract.llm import build_format_schema, load_extraction_schema
    fmt = build_format_schema(load_extraction_schema())
    variants = fmt["properties"]["items"]["items"]["anyOf"]
    flight = next(v for v in variants
                  if v["properties"]["item_type"].get("const") == "flight")
    detail = flight["properties"]["flight"]
    obj = next(c for c in (detail.get("anyOf") or [detail])
               if isinstance(c.get("properties"), dict) and "ticket_numbers" in c["properties"])
    assert "ticket_numbers" in obj["required"]


def test_jsonld_flight_regional_operator_and_reservation_id():
    """Regression (2026-08-04): a real Air Canada e-ticket carried a full
    schema.org FlightReservation, but its `provider` was the OPERATING regional
    (Jazz "QK") while the flightNumber was the marketing "AC9471". The handler
    passed QK to normalize_flight_number, which raised, so the ENTIRE
    deterministic extraction was discarded and the item fell to the LLM — which
    then under-read the times and airport codes that were sitting right there.
    The PNR also lived in reservationId, not reservationNumber."""
    doc = {
        "@context": "https://schema.org", "@type": "FlightReservation",
        "reservationId": "JMVRLE",
        "reservationStatus": "https://schema.org/ReservationConfirmed",
        "reservationFor": {
            "@type": "Flight", "flightNumber": "AC9471",
            "provider": {"@type": "Airline",
                         "name": "Air Canada Express - Jazz", "iataCode": "QK"},
            "departureAirport": {"@type": "Airport",
                "name": "Toronto-Pearson International Airport, Terminal 1",
                "iataCode": "YYZ"},
            "departureTime": "2026-11-20T07:20:00-05:00",
            "arrivalAirport": {"@type": "Airport",
                "name": "R. Reagan Washington National Airport, Terminal 1",
                "iataCode": "DCA"},
            "arrivalTime": "2026-11-20T08:58:00-05:00",
        },
    }
    items = jsonld.extract_jsonld(_ld_html(doc))
    assert len(items) == 1
    _assert_valid(items)
    p = items[0]
    f = p["flight"]
    assert f["flight_number"] == "8780"
    assert f["marketing_carrier"]["iata"] == "AC"       # from the number, not QK
    assert f["operating_carrier"]["iata"] == "QK"       # provider kept as operating
    assert f["departure"]["place"]["iata"] == "YYZ"
    assert f["departure"]["time"]["local"] == "2026-11-20T07:20"
    assert f["arrival"]["place"]["iata"] == "DCA"
    assert f["arrival"]["time"]["local"] == "2026-11-20T08:58"
    assert p["booking_reference"] == "JMVRLE"           # read from reservationId


def test_jsonld_lodging_and_graph():
    lodging = {
        "@type": "LodgingReservation", "reservationNumber": "HX-1",
        "reservationFor": {"@type": "LodgingBusiness", "name": "Test Hotel",
                           "address": {"addressLocality": "London", "addressCountry": "GB"}},
        "checkinTime": "2026-09-15T15:00:00", "checkoutTime": "2026-09-18T11:00:00",
    }
    items = jsonld.extract_jsonld(_ld_html({"@graph": [lodging]}))
    assert len(items) == 1
    _assert_valid(items)
    l = items[0]["lodging"]
    assert l["place"]["city"] == "London" and l["place"]["country"] == "GB"
    assert l["check_in"]["precision"] == "datetime"


def test_jsonld_ignores_junk():
    assert jsonld.extract_jsonld("<html><body>no markup</body></html>") == []
    assert jsonld.extract_jsonld(
        '<script type="application/ld+json">not json</script>') == []


# ---------------------------------------------------------------- ical

INVITE = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0", "METHOD:REQUEST", "BEGIN:VEVENT",
    "UID:abc123@google.com",
    "SUMMARY:Dinner w/Friends - usually 6:30",
    "LOCATION:Silver Diner\\, 123 Main St",
    "DTSTART;TZID=America/New_York:20260910T183000",
    "DTEND;TZID=America/New_York:20260910T203000",
    "ORGANIZER;CN=Someone:mailto:someone@example.com",
    "ATTENDEE;CN=Other:mailto:other@example.com",
    "END:VEVENT", "END:VCALENDAR"])

ALLDAY = "\r\n".join([
    "BEGIN:VCALENDAR", "BEGIN:VEVENT", "UID:x1",
    "SUMMARY:Dance Party", "DTSTART;VALUE=DATE:20260912",
    "DTEND;VALUE=DATE:20260913", "RRULE:FREQ=YEARLY",
    "END:VEVENT", "END:VCALENDAR"])


def test_ical_timed_event():
    items = ical.extract_ical(INVITE)
    assert len(items) == 1
    _assert_valid(items)
    e = items[0]["event"]
    assert e["start"] == {"local": "2026-09-10T18:30", "precision": "datetime",
                          "tz": "America/New_York", "origin": "document"}
    assert e["venue"]["raw"] == "Silver Diner, 123 Main St"
    assert e["organizer"] == "someone@example.com"
    assert e["attendees"] == ["other@example.com"]
    assert e["gcal_event_id"] == "abc123@google.com"
    assert items[0]["booking_status"] == "planned"       # an invite is a plan


def test_ical_allday_and_rrule():
    items = ical.extract_ical(ALLDAY)
    _assert_valid(items)
    e = items[0]["event"]
    assert e["start"]["precision"] == "date"             # never 00:00 (D-06)
    assert e["all_day"] is True
    assert e["gcal_recurring"] is True


# Outlook/Exchange invite: Windows TZID + a VTIMEZONE block whose STANDARD/
# DAYLIGHT sub-components carry their OWN DTSTART:16010101 and RRULE (the real
# shape of the DC Spirit Game / Wanita's invites that regressed).
INVITE_OUTLOOK = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0", "METHOD:REQUEST",
    "PRODID:Microsoft Exchange Server 2010",
    "BEGIN:VTIMEZONE", "TZID:Eastern Standard Time",
    "BEGIN:STANDARD", "DTSTART:16010101T020000",
    "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=1SU;BYMONTH=11",
    "TZOFFSETFROM:-0400", "TZOFFSETTO:-0500", "END:STANDARD",
    "BEGIN:DAYLIGHT", "DTSTART:16010101T020000",
    "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=2SU;BYMONTH=3",
    "TZOFFSETFROM:-0500", "TZOFFSETTO:-0400", "END:DAYLIGHT",
    "END:VTIMEZONE",
    "BEGIN:VEVENT", "UID:040000008200E00074C5B7101A82E008@exchange",
    "SUMMARY;LANGUAGE=en-US:DC Spirit Game/HH before- game at 7pm",
    "LOCATION;LANGUAGE=en-US:Audi Field",
    "DTSTART;TZID=Eastern Standard Time:20260726T160000",
    "DTEND;TZID=Eastern Standard Time:20260726T223000",
    "STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"])


def test_ical_outlook_windows_tz():
    """Outlook Windows zone names map to IANA instead of being dropped, and the
    VTIMEZONE block does not corrupt the event's date or recurrence flag."""
    items = ical.extract_ical(INVITE_OUTLOOK)
    assert len(items) == 1
    _assert_valid(items)
    e = items[0]["event"]
    assert e["start"] == {"local": "2026-07-26T16:00", "precision": "datetime",
                          "tz": "America/New_York", "origin": "document"}
    assert e["end"]["tz"] == "America/New_York"
    assert e["start"]["local"] == "2026-07-26T16:00"     # not the 1601 VTIMEZONE stub
    assert e["gcal_recurring"] is False                  # RRULE was inside VTIMEZONE
    assert items[0]["booking_status"] == "confirmed"


def test_ical_multisegment_iana_preserved():
    """Multi-segment IANA zones (Google/Apple) survive — the old regex dropped
    anything past the first '/'."""
    ics = INVITE.replace("America/New_York", "America/Argentina/Buenos_Aires")
    e = ical.extract_ical(ics)[0]["event"]
    assert e["start"]["tz"] == "America/Argentina/Buenos_Aires"


def test_ical_unknown_tzid_drops_to_none():
    """A TZID the tz database does not know resolves to None, never a guess."""
    ics = INVITE.replace("America/New_York", "Narnia/Cair_Paravel")
    e = ical.extract_ical(ics)[0]["event"]
    assert e["start"]["tz"] is None
    assert e["start"]["local"] == "2026-09-10T18:30"     # time itself still read


def test_windows_tz_to_iana():
    from extract.windows_tz import to_iana
    assert to_iana("Eastern Standard Time") == "America/New_York"
    assert to_iana("India Standard Time") == "Asia/Kolkata"
    assert to_iana('"Pacific Standard Time"') == "America/Los_Angeles"  # quoted
    assert to_iana("America/New_York") == "America/New_York"            # IANA passthrough
    assert to_iana("Not A Zone") is None
    assert to_iana(None) is None


# ---------------------------------------------------------------- gcal

GCAL_TIMED = {
    "id": "abc_20260901", "iCalUID": "abc@google.com", "status": "confirmed",
    "summary": "Dinner with Tricia", "location": "Silver Diner, 123 Main St",
    "description": "usually 6:30",
    "start": {"dateTime": "2026-09-01T18:30:00-04:00", "timeZone": "America/New_York"},
    "end": {"dateTime": "2026-09-01T20:30:00-04:00", "timeZone": "America/New_York"},
    "organizer": {"email": "tricia@example.com", "displayName": "Tricia"},
    "attendees": [{"email": "owner@example.com"}, {"displayName": "No Email"}],
}


def test_gcal_timed_event():
    from extract import gcal
    items = gcal.extract_gcal_event(GCAL_TIMED)
    _assert_valid(items)
    e = items[0]["event"]
    assert e["start"] == {"local": "2026-09-01T18:30", "precision": "datetime",
                          "tz": "America/New_York", "origin": "document"}
    assert e["end"]["tz"] == "America/New_York"
    assert e["venue"]["raw"].startswith("Silver Diner")
    assert e["organizer"] == "tricia@example.com"
    assert e["attendees"] == ["owner@example.com"]     # the email-less guest dropped
    assert e["gcal_event_id"] == "abc@google.com"          # iCalUID, not the instance id
    assert items[0]["booking_status"] == "confirmed"


def test_gcal_allday_recurring_cancelled():
    from extract import gcal
    ev = {"summary": "Team offsite", "status": "cancelled",
          "start": {"date": "2026-09-12"}, "end": {"date": "2026-09-13"},
          "recurrence": ["RRULE:FREQ=YEARLY"], "iCalUID": "x1"}
    e = gcal.extract_gcal_event(ev)[0]
    assert e["event"]["start"]["precision"] == "date"      # never 00:00 (D-06)
    assert e["event"]["all_day"] is True
    assert e["event"]["gcal_recurring"] is True
    assert e["booking_status"] == "cancelled"


def test_gcal_zulu_time_and_minimums():
    from extract import gcal
    e = gcal.extract_gcal_event(
        {"summary": "Call", "start": {"dateTime": "2026-09-01T14:00:00Z"}})[0]
    assert e["event"]["start"]["tz"] == "Etc/UTC"          # trailing Z -> UTC
    # a title AND a start are the minimum, same tolerance as ical.py
    assert gcal.extract_gcal_event({"summary": "No start"}) == []
    assert gcal.extract_gcal_event({"start": {"date": "2026-09-01"}}) == []


def test_pipeline_gcal_channel():
    r = pipeline.extract_ingest_payload(
        {"capture_channel": "gcal", "event": GCAL_TIMED}, llm=None)
    assert r.ok and r.extractor == "gcal"
    assert r.batch["items"][0]["item_type"] == "event"


# ---------------------------------------------------------------- maps_url

MAPS = ("https://www.google.com/maps/dir/Toronto,+ON/Port Calloway,+ON/Ottawa,+ON/"
        "@44.7,-77.0,8z/data=!3m1!4b1")


def test_maps_url_with_capture_date():
    items = maps_url.extract_maps_url(
        MAPS, {"travel_date": "2026-08-08", "timezone": "America/Toronto"})
    _assert_valid(items)
    g = items[0]["ground_transport"]
    assert g["mode"] == "car"
    assert g["departure"]["place"]["raw"] == "Toronto, ON"
    assert g["arrival"]["place"]["raw"] == "Ottawa, ON"
    assert [w["place"]["raw"] for w in g["waypoints"]] == ["Port Calloway, ON"]
    assert g["departure"]["time"]["precision"] == "date"          # no time given
    assert g["departure"]["time"]["origin"] == "capture_context"  # RULE-34
    assert g["route"]["is_estimate"] is True                      # RULE-27b


def test_maps_url_without_date_refuses():
    try:
        maps_url.extract_maps_url(MAPS, {})
    except maps_url.ExtractionError as ex:
        assert "RULE-31" in str(ex)
    else:
        raise AssertionError("should refuse to guess a date")


def test_maps_url_parses_city_region_for_geocoding():
    """A 'City, Region' stop is split so the geocoder's area tier can locate it
    (D-61) — the raw string alone does not geocode."""
    g = maps_url.extract_maps_url(MAPS, {"travel_date": "2026-08-08"})[0]["ground_transport"]
    dep = g["departure"]["place"]
    assert dep["city"] == "Toronto" and dep["region"] == "ON"
    assert g["arrival"]["place"]["city"] == "Ottawa"


def test_maps_url_api1_query_format():
    """The ?api=1 share format (waypoints pipe-separated) is accepted (D-61)."""
    url = ("https://www.google.com/maps/dir/?api=1&origin=Toronto,ON"
           "&destination=Ottawa,ON&waypoints=Port Calloway,ON|Ashcombe,ON")
    g = maps_url.extract_maps_url(url, {"travel_date": "2026-08-08"})[0]["ground_transport"]
    assert g["departure"]["place"]["city"] == "Toronto"
    assert g["arrival"]["place"]["city"] == "Ottawa"
    assert [w["place"]["city"] for w in g["waypoints"]] == ["Port Calloway", "Ashcombe"]


def test_maps_url_coords_stay_out_of_extraction():
    """The extraction Place schema has NO coord fields — coords ride in
    route.source_url and are applied at resolve, so extraction stays schema-valid
    (D-61). This is the regression guard for the schema-invalid coord bug."""
    url = ("https://www.google.com/maps/dir/"
           "2074+Donald+Harbour+Pkwy,+Locust+Hill,+ON+L0H+1J0,+Canada/Port Calloway,+Ontario/"
           "@44.07,-78.5,9z/data=!3m1!4b1!1m5!1m1!1s0xabc:0xdef!2m2!1d-79.3832123!2d43.6532456"
           "!1m5!1m1!1s0x123:0x456!2m2!1d-76.4930295!2d44.2334401!3e0")
    items = maps_url.extract_maps_url(url, {"travel_date": "2026-08-14"})
    _assert_valid(items)                                       # schema-valid
    gt = items[0]["ground_transport"]
    assert "lat" not in gt["departure"]["place"]              # coords NOT at extraction
    assert "!1d-79.3832123!2d43.6532456" in gt["route"]["source_url"]   # they ride in the URL


def test_maps_url_coordinate_segment_kept_as_raw():
    url = "https://www.google.com/maps/dir/43.6532,-79.3832/45.4215,-75.6972"
    items = maps_url.extract_maps_url(url, {"travel_date": "2026-08-08"})
    _assert_valid(items)
    dep = items[0]["ground_transport"]["departure"]["place"]
    assert dep["raw"] == "43.6532,-79.3832" and "lat" not in dep   # coord read at resolve


def test_maps_url_multiday_uses_end_date_for_arrival():
    g = maps_url.extract_maps_url(
        MAPS, {"travel_date": "2026-08-08", "end_date": "2026-08-10"})[0]["ground_transport"]
    assert g["departure"]["time"]["local"] == "2026-08-08"
    assert g["arrival"]["time"]["local"] == "2026-08-10"


def test_maps_url_short_link_host_guard():
    """Only Google short-link hosts are ever fetched (SSRF guard): an arbitrary
    host is not expanded, so it simply fails as 'not a directions URL'."""
    try:
        maps_url.extract_maps_url("https://evil.example.com/x",
                                  {"travel_date": "2026-08-08"})
    except maps_url.ExtractionError as ex:
        assert "directions URL" in str(ex)
    else:
        raise AssertionError("should not fetch or accept an arbitrary host")


# ---------------------------------------------------------------- pdf

def _tiny_pdf(text: str) -> bytes:
    """Hand-assembled single-page PDF with a correct xref table."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF").encode()
    return out


def test_pdf_text_extraction():
    try:
        import pypdf  # noqa: F401
    except ImportError:
        print("  (pypdf not installed on this host — pdf test runs in container/CI)")
        return
    from extract.pdf import extract_all_pdfs, extract_pdf_text
    pdf_bytes = _tiny_pdf("Amtrak eTicket 1234")
    assert "Amtrak eTicket 1234" in extract_pdf_text(pdf_bytes)
    assert extract_pdf_text(b"not a pdf") == ""
    named = extract_all_pdfs([("t.pdf", pdf_bytes), ("bad.pdf", b"junk")])
    assert list(named) == ["t.pdf"]



# ---------------------------------------------------------------- normalize

def test_normalize_fills_iata_from_raw_airport_code():
    """Measured on real data: the LLM copies "YYZ" into Place.raw and leaves
    iata empty, which broke key generation for four United legs (and RULE-03)."""
    from extract.normalize import normalize_payload
    p = {"item_type": "flight", "booking_status": "confirmed",
         "flight": {"flight_number": "3637",
                    "departure": {"place": {"raw": "YYZ"},
                                  "time": {"local": "2026-10-02T14:30",
                                           "precision": "datetime"}},
                    "arrival": {"place": {"raw": "Dulles Intl (IAD)"},
                                "time": {"local": "2026-10-02T16:02",
                                         "precision": "datetime"}}}}
    out = normalize_payload(p)
    assert out["flight"]["departure"]["place"]["iata"] == "YYZ"
    assert out["flight"]["arrival"]["place"]["iata"] == "IAD"   # parsed from parens
    assert p["flight"]["departure"]["place"].get("iata") is None  # input untouched


def test_normalize_does_not_invent_codes_for_non_airports():
    from extract.normalize import normalize_payload
    p = {"item_type": "lodging", "booking_status": "confirmed",
         "lodging": {"property_name": "INN", "place": {"raw": "INN"},
                     "check_in": {"local": "2026-01-01", "precision": "date"},
                     "check_out": {"local": "2026-01-02", "precision": "date"}}}
    out = normalize_payload(p)
    assert "iata" not in out["lodging"]["place"]     # a hotel is not an airport


def test_normalize_drops_malformed_country():
    from extract.normalize import normalize_place
    assert normalize_place({"raw": "X", "country": "usa"}, is_airport=False).get("country") is None
    assert normalize_place({"raw": "X", "country": "us"}, is_airport=False)["country"] == "US"

# ---------------------------------------------------------------- llm (mocked)

VALID_BATCH = {"items": [{"item_type": "event", "booking_status": "confirmed",
                          "event": {"title": "Show",
                                    "start": {"local": "2026-09-10",
                                              "precision": "date"}}}],
               "declared_segment_count": None}


class _FakeLLM(OllamaExtractor):
    def __init__(self, responses):
        super().__init__(base_url="http://fake", model="fake")
        self._responses = list(responses)
        self.calls = 0

    def _post(self, messages):
        self.calls += 1
        return self._responses.pop(0)


def test_llm_retry_then_success():
    llm = _FakeLLM(["{broken json", json.dumps(VALID_BATCH)])
    batch, attempts = llm.extract("doc")
    assert batch == VALID_BATCH
    assert llm.calls == 2 and attempts[-1]["ok"] is True


def test_llm_two_failures_raises():
    llm = _FakeLLM([json.dumps({"items": []}),          # violates minItems
                    json.dumps({"nope": 1})])
    try:
        llm.extract("doc")
    except ExtractionFailed as ex:
        assert llm.calls == 2                            # exactly one retry (D-11)
        assert len(ex.attempts) == 2
    else:
        raise AssertionError("should have raised after single retry")


def test_inline_refs_leaves_no_refs():
    inlined = inline_refs(SCHEMA)
    assert "$ref" not in json.dumps(inlined)


def test_format_schema_forces_detail_objects():
    """Regression for the measured 11/14 grammar failure: item_type set but
    detail object omitted. Each typed variant must hard-require its detail."""
    from extract.llm import build_format_schema
    fmt = build_format_schema(SCHEMA)
    s = json.dumps(fmt)
    assert "$ref" not in s and '"if"' not in s          # grammar-safe
    variants = fmt["properties"]["items"]["items"]["anyOf"]
    by_type = {}
    for v in variants:
        it = v["properties"]["item_type"]
        key = it.get("const") or tuple(it.get("enum", []))
        by_type[key] = v
    for t in ("flight", "ground_transport", "lodging", "event"):
        assert t in by_type[t]["required"], t
    assert ("note", "unknown") in by_type                 # detail-less variant exists
    for v in variants:                    # nullable-but-required: forces the model
        assert "booking_reference" in v["required"]       # to consciously emit it


# ---------------------------------------------------------------- pipeline

def test_pipeline_jsonld_short_circuits_llm():
    payload = {"capture_channel": "gmail",
               "raw_message_base64": _email_b64(html=_ld_html(FLIGHT_LD))}
    r = pipeline.extract_ingest_payload(payload, llm=None)   # no LLM needed
    assert r.ok and r.extractor == "jsonld"
    assert r.batch["items"][0]["item_type"] == "flight"


def test_pipeline_ical():
    payload = {"capture_channel": "gmail",
               "raw_message_base64": _email_b64(ical_text=INVITE)}
    r = pipeline.extract_ingest_payload(payload, llm=None)
    assert r.ok and r.extractor == "ical"


def test_pipeline_plain_email_needs_llm():
    payload = {"capture_channel": "gmail",
               "raw_message_base64": _email_b64(html="<p>Your booking is confirmed.</p>")}
    r = pipeline.extract_ingest_payload(payload, llm=None)
    assert r.needs_review and not r.ok
    assert any("no LLM configured" in x for x in r.reasons)


def test_pipeline_llm_path_with_mock():
    payload = {"capture_channel": "gmail",
               "raw_message_base64": _email_b64(html="<p>Show ticket enclosed</p>")}
    r = pipeline.extract_ingest_payload(payload, llm=_FakeLLM([json.dumps(VALID_BATCH)]))
    assert r.ok and r.extractor == "llm"


def test_pipeline_maps_channel():
    r = pipeline.extract_ingest_payload(
        {"capture_channel": "maps_url", "url": MAPS,
         "capture_context": {"travel_date": "2026-08-08"}})
    assert r.ok and r.extractor == "maps_url"
    r2 = pipeline.extract_ingest_payload(
        {"capture_channel": "maps_url", "url": MAPS, "capture_context": {}})
    assert r2.needs_review


# ---------------------------------------------------------- google mode select

def test_maps_url_mode_selection_walk():
    """A capture-selected walk mode is written through (SPEC-4 §3)."""
    g = maps_url.extract_maps_url(
        MAPS, {"travel_date": "2026-08-08", "mode": "walk"})[0]["ground_transport"]
    assert g["mode"] == "walk"


def test_maps_url_mode_conflict_rejected():
    """travelmode=walking in the URL contradicts a Car selection -> refuse."""
    url = ("https://www.google.com/maps/dir/?api=1&origin=Toronto,ON"
           "&destination=Ottawa,ON&travelmode=walking")
    try:
        maps_url.extract_maps_url(url, {"travel_date": "2026-08-08", "mode": "car"})
    except maps_url.ExtractionError as ex:
        assert "mismatch" in str(ex)
    else:
        raise AssertionError("should refuse a mode that contradicts the URL")


def test_maps_url_transit_gets_notes_marker():
    """PARITY: a Google transit route -> mode other + a notes marker, exactly
    like Apple (shared builder, D-89)."""
    url = ("https://www.google.com/maps/dir/?api=1&origin=Toronto,ON"
           "&destination=Ottawa,ON&travelmode=transit")
    items = maps_url.extract_maps_url(url, {"travel_date": "2026-08-08", "mode": "transit"})
    _assert_valid(items)
    assert items[0]["ground_transport"]["mode"] == "other"
    assert "transit" in (items[0]["notes"] or "").lower()
    assert "Google Maps" in items[0]["notes"]


def test_maps_url_current_location_origin_refuses():
    """PARITY: a current-location origin is rejected for Google too (D-89)."""
    url = ("https://www.google.com/maps/dir/?api=1&origin=Current Location"
           "&destination=Ottawa,ON")
    try:
        maps_url.extract_maps_url(url, {"travel_date": "2026-08-08"})
    except maps_url.ExtractionError as ex:
        assert "current-location" in str(ex) or "placeholder" in str(ex)
    else:
        raise AssertionError("a current-location origin is not stable — refuse")


# ---------------------------------------------------------------- apple_maps_url

APPLE = ("https://maps.apple.com/?saddr=Toronto,+ON&daddr=Ottawa,+ON&dirflg=d")


def test_apple_maps_url_happy_path_car():
    items = apple_maps_url.extract_apple_maps_url(
        APPLE, {"travel_date": "2026-08-08", "timezone": "America/Toronto"})
    _assert_valid(items)
    g = items[0]["ground_transport"]
    assert g["mode"] == "car"
    assert g["departure"]["place"]["city"] == "Toronto"
    assert g["arrival"]["place"]["city"] == "Ottawa"
    assert g["waypoints"] == []                                   # Apple: no waypoints
    assert g["route"]["provider"] == "apple_maps"
    assert g["route"]["is_estimate"] is True
    assert g["departure"]["time"]["origin"] == "capture_context"  # RULE-34


def test_apple_maps_url_walking_mode():
    url = "https://maps.apple.com/?saddr=A&daddr=B&dirflg=w"
    g = apple_maps_url.extract_apple_maps_url(
        url, {"travel_date": "2026-08-08", "mode": "walk"})[0]["ground_transport"]
    assert g["mode"] == "walk"


def test_apple_maps_url_transit_is_other_with_notes_marker():
    url = "https://maps.apple.com/?saddr=A&daddr=B&dirflg=r"
    items = apple_maps_url.extract_apple_maps_url(
        url, {"travel_date": "2026-08-08", "mode": "transit"})
    _assert_valid(items)
    assert items[0]["ground_transport"]["mode"] == "other"
    assert "transit" in (items[0]["notes"] or "").lower()


def test_apple_maps_url_mode_conflict_rejected():
    """dirflg=w (walking) contradicts a Car selection -> refuse (D-91 revised)."""
    url = "https://maps.apple.com/?saddr=A&daddr=B&dirflg=w"
    try:
        apple_maps_url.extract_apple_maps_url(url, {"travel_date": "2026-08-08", "mode": "car"})
    except apple_maps_url.ExtractionError as ex:
        assert "mismatch" in str(ex)
    else:
        raise AssertionError("should refuse a mode that contradicts dirflg")


def test_apple_maps_url_absent_dirflg_is_not_a_conflict():
    """No dirflg + Car selection is fine — an absent signal never conflicts."""
    url = "https://maps.apple.com/?saddr=A&daddr=B"
    g = apple_maps_url.extract_apple_maps_url(
        url, {"travel_date": "2026-08-08", "mode": "car"})[0]["ground_transport"]
    assert g["mode"] == "car"


def test_apple_maps_url_unknown_dirflg_rejected():
    """An unrecognized dirflg (e.g. cycling) is a present, unsupported signal."""
    url = "https://maps.apple.com/?saddr=A&daddr=B&dirflg=c"
    try:
        apple_maps_url.extract_apple_maps_url(url, {"travel_date": "2026-08-08", "mode": "car"})
    except apple_maps_url.ExtractionError as ex:
        assert "mismatch" in str(ex)
    else:
        raise AssertionError("should refuse an unrecognized dirflg")


def test_apple_maps_url_daddr_only_refuses():
    url = "https://maps.apple.com/?daddr=Ottawa,+ON"
    try:
        apple_maps_url.extract_apple_maps_url(url, {"travel_date": "2026-08-08"})
    except apple_maps_url.ExtractionError as ex:
        assert "2 stops" in str(ex)
    else:
        raise AssertionError("a link with only daddr must refuse")


def test_apple_maps_url_current_location_origin_refuses():
    url = "https://maps.apple.com/?saddr=Current%20Location&daddr=Ottawa,+ON"
    try:
        apple_maps_url.extract_apple_maps_url(url, {"travel_date": "2026-08-08"})
    except apple_maps_url.ExtractionError as ex:
        assert "current-location" in str(ex) or "placeholder" in str(ex)
    else:
        raise AssertionError("a current-location origin is not stable — refuse")


def test_apple_maps_url_without_date_refuses():
    try:
        apple_maps_url.extract_apple_maps_url(APPLE, {})
    except apple_maps_url.ExtractionError as ex:
        assert "RULE-31" in str(ex)
    else:
        raise AssertionError("should refuse to guess a date")


def test_apple_maps_url_coordinate_saddr_parses():
    url = "https://maps.apple.com/?saddr=37.7749,-122.4194&daddr=Ottawa,+ON"
    g = apple_maps_url.extract_apple_maps_url(
        url, {"travel_date": "2026-08-08"})[0]["ground_transport"]
    assert g["departure"]["place"]["raw"] == "37.7749,-122.4194"
    assert "city" not in g["departure"]["place"]                  # a coord, not a name
    assert g["arrival"]["place"]["city"] == "Ottawa"


def test_apple_maps_url_short_link_refuses():
    try:
        apple_maps_url.extract_apple_maps_url(
            "https://maps.apple/p/abc123", {"travel_date": "2026-08-08"})
    except apple_maps_url.ExtractionError as ex:
        assert "short link" in str(ex)
    else:
        raise AssertionError("a maps.apple/p short link is out of scope (D-88)")


def test_looks_like_apple_maps_url():
    assert apple_maps_url.looks_like_apple_maps_url(APPLE)
    assert not apple_maps_url.looks_like_apple_maps_url("https://maps.apple/p/abc")
    assert not apple_maps_url.looks_like_apple_maps_url(
        "https://maps.apple.com/?daddr=Ottawa")                   # missing saddr
    assert not apple_maps_url.looks_like_apple_maps_url(
        "https://www.google.com/maps/dir/A/B")
    assert apple_maps_url.is_apple_short_link("https://maps.apple/p/abc")


def test_pipeline_routes_apple_before_google():
    """An Apple link reaches the apple_maps_url extractor, not the Google one."""
    r = pipeline.extract_ingest_payload(
        {"capture_channel": "apple_maps_url", "url": APPLE,
         "capture_context": {"travel_date": "2026-08-08"}})
    assert r.ok and r.extractor == "apple_maps_url"
    # Also via bare-URL sniffing (no explicit channel).
    r2 = pipeline.extract_ingest_payload(
        {"url": APPLE, "capture_context": {"travel_date": "2026-08-08"}})
    assert r2.ok and r2.extractor == "apple_maps_url"


# ---------------------------------------------------------------- runner

if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as ex:
                failures += 1
                print(f"  FAIL {name}: {str(ex)[:300]}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
