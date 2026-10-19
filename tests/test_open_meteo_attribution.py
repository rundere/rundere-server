"""Open-Meteo attribution — its weather data is CC BY 4.0 and must be credited
"Weather data by Open-Meteo.com" (linking https://open-meteo.com/) wherever
it's shown: the Discover weather section on the web and the API snapshot the
iOS app reads (MCP's get_trip is covered in test_mcp_http). A self-hosted
TX_OPEN_METEO_URL mirror still serves Open-Meteo's data, so it keeps the credit.
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discover.resolvers.weather as weather_mod  # noqa: E402
from discover import attribution, sections  # noqa: E402
from discover.places import DerivedPlace  # noqa: E402
from discover.resolvers.base import ResolverContext  # noqa: E402

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

TEXT = "Weather data by Open-Meteo.com"
URL = "https://open-meteo.com/"


def test_open_meteo_weather_carries_the_credit():
    a = attribution.for_finding("weather.openmeteo")
    assert a == {"text": TEXT, "url": URL, "license": "CC BY 4.0"}
    a["text"] = "mutated"                       # callers get a copy
    assert attribution.for_finding("weather.openmeteo")["text"] == TEXT


def test_other_resolvers_need_no_credit():
    for r in (None, "", "agent", "currency.frankfurter", "plugs.static"):
        assert attribution.for_finding(r) is None


def test_a_self_hosted_mirror_keeps_the_credit(monkeypatch):
    """The credit follows the resolver, not the URL it was pointed at."""
    seen = []

    def fake_get_json(url, timeout=12):
        seen.append(url)
        return {"daily": {"temperature_2m_max": [20, 22], "temperature_2m_min": [10, 12],
                          "precipitation_sum": [0, 0]}}
    monkeypatch.setattr(weather_mod, "get_json", fake_get_json)
    r = weather_mod.WeatherResolver(forecast_url="http://weather.example.test/v1",
                                    archive_url="http://weather.example.test/v1")
    f = r.resolve(DerivedPlace(country="PT", city="Lisbon", lat=38.7, lon=-9.1),
                  ResolverContext(start_date="2026-10-20", end_date="2026-10-21",
                                  today=date(2026, 10, 19).isoformat()))
    assert seen and seen[0].startswith("http://weather.example.test/")
    assert attribution.for_finding(f.resolver)["text"] == TEXT


def test_sections_credit_each_source_once_and_only_where_shown():
    a = attribution.for_finding("weather.openmeteo")
    secs = {s["key"]: s for s in sections.build({
        "weather": [{"attribution": a}, {"attribution": dict(a)}],
        "food": [{"attribution": None}]})}
    assert secs["weather"]["attributions"] == [a]
    assert secs["food"]["attributions"] == [] and secs["practical"]["attributions"] == []


if DEPS:
    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, DiscoveryFinding, Trip, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    _W = {"high_c": 24, "low_c": 15, "rain_days": 0, "days": 4, "mode": "forecast"}

    def _setup(with_weather=True):
        with SessionLocal() as s:
            _teardown(s)
            me = User(username="om-attr", auth_source="local",
                      password_hash=authn.hash_password("pw"))
            s.add(me); s.flush()
            t = Trip(name="OM Attribution", source="manual", owner_id=me.id,
                     start_date="2027-04-01", end_date="2027-04-04")
            s.add(t); s.flush()
            rows = [DiscoveryFinding(trip_id=t.id, user_id=me.id, topic="food", scope="city",
                                     place_key="PT:Lisbon", status="draft", resolver="agent",
                                     body_md="om-food-notes", data={"agent": "Claude"})]
            if with_weather:
                rows.append(DiscoveryFinding(
                    trip_id=t.id, user_id=me.id, topic="weather", scope="city",
                    place_key="PT:Lisbon", status="draft", resolver="weather.openmeteo",
                    body_md="**Lisbon, PT** — forecast", data=_W,
                    sources=[{"title": "Open-Meteo", "url": "https://open-meteo.com"}]))
            s.add_all(rows); s.commit()
            return me.id, t.id

    def _teardown(s):
        tids = [t.id for t in s.query(Trip).filter_by(name="OM Attribution").all()]
        s.query(DiscoveryFinding).filter(DiscoveryFinding.trip_id.in_(tids)).delete(
            synchronize_session=False)
        s.query(Trip).filter(Trip.id.in_(tids)).delete(synchronize_session=False)
        s.query(User).filter_by(username="om-attr").delete(synchronize_session=False)
        s.commit()

    def _h(uid):
        with SessionLocal() as s:
            return {"Authorization": f"Bearer {authn.issue_access_token(s.get(User, uid))}"}

    def _page(uid, tid):
        settings.auth_required = True
        try:
            r = client.post("/login", data={"username": "om-attr", "password": "pw"},
                            follow_redirects=False)
            assert r.status_code in (302, 303), r.text
            return client.get(f"/trips/{tid}").text
        finally:
            settings.auth_required = False
            client.cookies.clear()

    def test_web_weather_section_shows_the_credit():
        uid, tid = _setup()
        try:
            page = _page(uid, tid)
            assert "Warm and mostly dry" in page          # the weather section rendered
            assert (f'<a href="{URL}" target="_blank" rel="noopener">{TEXT}</a>'
                    f' · CC BY 4.0') in page
            assert page.count(TEXT) == 1                  # once, under Weather only
        finally:
            with SessionLocal() as s:
                _teardown(s)

    def test_web_without_weather_shows_no_credit():
        uid, tid = _setup(with_weather=False)
        try:
            page = _page(uid, tid)
            assert "om-food-notes" in page and TEXT not in page
        finally:
            with SessionLocal() as s:
                _teardown(s)

    def test_api_snapshot_carries_the_credit_on_weather_only():
        uid, tid = _setup()
        try:
            snap = client.get(f"/api/v1/trips/{tid}", headers=_h(uid)).json()
            by_topic = {f["topic"]: f for f in snap["discovery"]}
            assert by_topic["weather"]["attribution"] == {
                "text": TEXT, "url": URL, "license": "CC BY 4.0"}
            assert by_topic["food"]["attribution"] is None
        finally:
            with SessionLocal() as s:
                _teardown(s)

    def test_attribution_is_in_the_openapi_contract():
        uid, tid = _setup(with_weather=False)
        try:
            spec = client.get("/api/v1/openapi.json", headers=_h(uid)).json()
            props = spec["components"]["schemas"]["FindingOut"]["properties"]
            assert "attribution" in props
            assert {"text", "url", "license"} <= set(
                spec["components"]["schemas"]["AttributionOut"]["properties"])
        finally:
            with SessionLocal() as s:
                _teardown(s)
