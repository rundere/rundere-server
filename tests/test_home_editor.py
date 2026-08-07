"""Home Profiles editor (D-39). Uses the FastAPI test client; AirTrail/Photon
lookups are exercised live only in the container, so these cover the parts that
don't need those services: dropdown, validation, save/edit/delete, and the
lookup-result field-fill fragment."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, HomeProfile
    Base.metadata.create_all(engine)
    client = TestClient(app)


def _skip_or(fn):
    if not DEPS:
        import pytest
        pytest.skip("fastapi not installed on this host")
    return fn()


def _clear():
    with SessionLocal() as s:
        s.query(HomeProfile).delete()
        s.commit()


def test_kind_is_a_dropdown_not_free_text():
    def run():
        html = client.get("/review/home").text
        assert '<select id="hp-kind"' in html
        assert "Home airport" in html and "Residence" in html
    _skip_or(run)


def test_kind_fields_swap_is_conditional():
    def run():
        air = client.get("/review/home/kind-fields?kind=home_airport").text
        assert "Airport code" in air
        res = client.get("/review/home/kind-fields?kind=residence").text
        assert "Address or city" in res and "Home radius" in res
    _skip_or(run)


def test_home_airport_requires_a_code():
    def run():
        _clear()
        r = client.post("/review/home",
                        data={"kind": "home_airport", "label": "X", "iata": ""})
        assert "3-letter airport code" in r.text
        with SessionLocal() as s:
            assert s.query(HomeProfile).count() == 0     # nothing saved
    _skip_or(run)


def test_residence_requires_coordinates():
    def run():
        _clear()
        r = client.post("/review/home",
                        data={"kind": "residence", "label": "Home",
                              "lat": "", "lon": ""})
        assert "needs coordinates" in r.text
        with SessionLocal() as s:
            assert s.query(HomeProfile).count() == 0
    _skip_or(run)


def test_save_edit_delete_roundtrip():
    def run():
        _clear()
        client.post("/review/home", data={
            "kind": "home_airport", "label": "Pearson", "iata": "yyz",
            "lat": "43.6759", "lon": "-79.6294", "country": "ca",
            "effective_from": "2020-01-01", "notes": "main"})
        with SessionLocal() as s:
            p = s.query(HomeProfile).one()
            assert p.iata == "YYZ" and p.country == "CA"   # normalised upper
            assert p.kind == "home_airport" and p.notes == "main"
            pid = p.id
        # edit prefills
        edit = client.get(f"/review/home?edit={pid}").text
        assert "Edit home" in edit and 'value="YYZ"' in edit
        # update
        client.post("/review/home", data={
            "edit_id": str(pid), "kind": "home_airport", "label": "Pearson 2",
            "iata": "YYZ", "lat": "43.6759", "lon": "-79.6294", "country": "CA"})
        with SessionLocal() as s:
            assert s.query(HomeProfile).one().label == "Pearson 2"
        # delete
        client.post(f"/review/home/delete/{pid}")
        with SessionLocal() as s:
            assert s.query(HomeProfile).count() == 0
    _skip_or(run)


def test_residence_saves_with_looked_up_coordinates():
    def run():
        _clear()
        client.post("/review/home", data={
            "kind": "residence", "label": "Home City", "lat": "43.65",
            "lon": "-79.38", "country": "CA", "radius_km": "50"})
        with SessionLocal() as s:
            p = s.query(HomeProfile).one()
            assert p.kind == "residence" and p.iata is None
            assert p.lat == 43.65 and p.radius_km == 50.0
    _skip_or(run)


def test_lookup_result_fragment_fills_fields():
    """The lookup response must carry a script that populates the coordinate
    inputs — otherwise the operator would still be transcribing."""
    def run():
        # residence address lookup would call Photon; exercise the airport
        # branch's not-found path instead, which needs no external service.
        # Success path (with a script) is covered live in the container.
        from app.review.home import _lookup_result
        from starlette.requests import Request
        # build a minimal request scope for the template
        scope = {"type": "http", "method": "POST", "headers": [], "query_string": b""}
        html = _lookup_result(Request(scope),
                              found={"label": "Toronto Pearson (YYZ)",
                                     "iata": "YYZ", "lat": 43.6759,
                                     "lon": -79.6294, "country": "CA",
                                     "source": "AirTrail"}).body.decode()
        assert "set('hp-lat'" in html and "set('hp-country'" in html
        assert "43.6759" in html and "YYZ" in html
    _skip_or(run)


if __name__ == "__main__":
    if not DEPS:
        print("SKIP: fastapi not installed on this host")
        sys.exit(0)
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
