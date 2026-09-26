"""Trip maps in the timeline PDF (app/trips/maps.py + app/trips/pdf.py).

Rendering runs for real (Pillow); map tiles are stubbed, so no network.
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import sqlalchemy  # noqa: F401
    from PIL import Image
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app.db import SessionLocal, engine
    from app.models import Base, Trip, TripActivity
    from app.trips import maps
    Base.metadata.create_all(engine)

PHX, SED, YYZ = (35.0402, -106.609), (35.687, -105.938), (43.6777, -79.6248)


def test_world_pixels_match_the_standard_tile_scheme():
    if not DEPS:
        return
    x, y = maps._world(*PHX, 7)
    assert (int(x // 256), int(y // 256)) == (26, 50)       # tile 7/26/50 covers PHX


def test_fit_frames_every_point_inside_the_image():
    if not DEPS:
        return
    for pts in ([PHX, SED], [PHX, SED, YYZ], [PHX]):
        c = maps._fit(pts, 680, 400)
        for p in pts:
            x, y = c.xy(*p)
            assert 0 < x < 680 * maps.SCALE and 0 < y < 400 * maps.SCALE
    # fractional zoom: a pair fills most of the frame, not a third of it
    c = maps._fit([PHX, SED], 680, 400)
    (x1, y1), (x2, y2) = c.xy(*PHX), c.xy(*SED)
    assert max(abs(x2 - x1) / (680 * maps.SCALE), abs(y2 - y1) / (400 * maps.SCALE)) > 0.7


def test_great_circle_ends_at_both_airports():
    if not DEPS:
        return
    arc = maps.great_circle(YYZ, PHX)
    assert abs(arc[0][0] - YYZ[0]) < 1e-6 and abs(arc[-1][1] - PHX[1]) < 1e-6
    assert len(arc) == 49


def test_schematic_map_without_tiles(monkeypatch):
    if not DEPS:
        return
    from app.config import settings
    monkeypatch.setattr(settings, "map_tile_url", "")
    img = maps.render_map([maps.Marker(*PHX, "1"), maps.Marker(*SED, "2")],
                          [maps.Line([PHX, SED])], width=400, height=200)
    assert img.mime == "image/png" and img.attribution == ""
    assert Image.open(io.BytesIO(img.data)).size == (800, 400)       # 2x for print
    assert maps.render_map([], [], width=400, height=200) is None


def test_tiled_map_uses_the_tile_source_and_credits_it(monkeypatch):
    if not DEPS:
        return
    from app.config import settings
    monkeypatch.setattr(settings, "map_tile_url", "https://tiles.example/{z}/{x}/{y}.png")
    monkeypatch.setattr(settings, "map_tile_attribution", "© Example")
    asked = []
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), (200, 220, 200)).save(buf, "PNG")

    def fake_tile(template, z, x, y):
        asked.append((z, x, y))
        return buf.getvalue()
    monkeypatch.setattr(maps, "_tile", fake_tile)
    img = maps.render_map([maps.Marker(*PHX, "A"), maps.Marker(*SED, "B")],
                          [maps.Line([PHX, SED])], width=560, height=190)
    assert img.mime == "image/jpeg" and img.attribution == "© Example"
    assert asked and all(z == asked[0][0] for z, _, _ in asked)
    # a failing tile server falls back to the schematic, uncredited
    monkeypatch.setattr(maps, "_tile", lambda *a: None)
    img = maps.render_map([maps.Marker(*PHX, "A")], [], width=300, height=200)
    assert img.mime == "image/png" and img.attribution == ""


def test_overlapping_pins_merge():
    if not DEPS:
        return
    c = maps._fit([PHX, SED], 680, 400)
    near = (PHX[0] + 0.001, PHX[1] + 0.001)
    merged = maps._cluster([maps.Marker(*PHX, "1"), maps.Marker(*near, "4"),
                            maps.Marker(*near, ""), maps.Marker(*SED, "5")], c)
    assert [m.label for m in merged] == ["1,4", "5"]


def _pt(role, label, lat, lon):
    return {"role": role, "label": label, "lat": lat, "lon": lon, "source": "photon"}


def test_pdf_has_trip_flight_and_road_trip_maps(monkeypatch):
    if not DEPS:
        return
    from app.config import settings
    from app.trips.pdf import render_timeline_html, timeline_context
    from resolve.routing import encode_polyline
    monkeypatch.setattr(settings, "map_tile_url", "")
    with SessionLocal() as s:
        t = Trip(name="PDF maps test", start_date="2027-03-01", end_date="2027-03-05")
        s.add(t)
        s.flush()
        s.add_all([
            TripActivity(trip_id=t.id, type="flight", starts_at="2027-03-01T08:00",
                         values={"when": "2027-03-01T08:00", "from": "YYZ", "to": "PHX"},
                         geo={"points": [_pt("from", "YYZ", *YYZ), _pt("to", "PHX", *PHX)]}),
            TripActivity(trip_id=t.id, type="lodging", starts_at="2027-03-01T15:00",
                         values={"when": "2027-03-01T15:00", "name": "Hotel Palo Verde"},
                         geo={"points": [_pt("place", "Hotel Palo Verde", 35.0985, -106.6645)]}),
            TripActivity(trip_id=t.id, type="drive", starts_at="2027-03-02T10:00",
                         values={"when": "2027-03-02T10:00", "route": "PHX → Sedona"},
                         geo={"points": [_pt("from", "Sunport", *PHX),
                                         _pt("to", "Sedona Plaza", *SED)],
                              "route": {"polyline": encode_polyline([PHX, (35.3, -106.4), SED])}}),
            TripActivity(trip_id=t.id, type="note", starts_at="2027-03-03",
                         values={"title": "No place"}),
        ])
        s.commit()
        try:
            ctx = timeline_context(s, t)
            assert ctx["trip_map"].startswith("data:image/png;base64,")
            assert ctx["flight_map"].startswith("data:image/png;base64,")
            acts = [a for d in ctx["days"] for a in d["activities"]]
            nos = {a["type"]: a.get("map_no") for a in acts}
            assert nos == {"flight": None, "lodging": 1, "drive": 2, "note": None}
            drive = next(a for a in acts if a["type"] == "drive")
            assert drive["route_map"].startswith("data:image/png;base64,")
            assert {"Lodging", "Road trip"} <= {l["label"] for l in ctx["legend"]}
            html = render_timeline_html(s, t)
            assert "Trip map" in html and "Getting there" in html
            assert 'class="route-map"' in html
        finally:
            s.query(TripActivity).filter(TripActivity.trip_id == t.id).delete()
            s.delete(t)
            s.commit()


def test_pdf_without_located_activities_has_no_map_section():
    if not DEPS:
        return
    from app.trips.pdf import render_timeline_html
    with SessionLocal() as s:
        t = Trip(name="PDF no maps", start_date="2027-04-01", end_date="2027-04-02")
        s.add(t)
        s.flush()
        s.add(TripActivity(trip_id=t.id, type="dining", starts_at="2027-04-01T19:00",
                           values={"when": "2027-04-01T19:00", "name": "Somewhere"}))
        s.commit()
        try:
            html = render_timeline_html(s, t)
            assert "Trip map" not in html and "Getting there" not in html
        finally:
            s.query(TripActivity).filter(TripActivity.trip_id == t.id).delete()
            s.delete(t)
            s.commit()
