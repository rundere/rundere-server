"""Importing places from a Run·dere CSV file (SPEC-11 D-243, YONDER-374): the
preview writes nothing, re-importing adds nothing, lists are matched or made per
person, a bad row never stops the rest, and the web page, API and MCP share one
import. Every person, place, list and address here is invented. SQLite via
conftest."""
import csv
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

try:
    from fastapi.testclient import TestClient
    from app import authn, mcp_server, places as places_svc, places_import
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, PlaceList, PlaceListMember, SavedPlace, User
    Base.metadata.create_all(engine)
    client = TestClient(app)
except ImportError:                                     # bare host: the container/CI runs these
    pytest.skip("fastapi not installed", allow_module_level=True)

HEADER = ("name", "url", "address", "lat", "lon", "list", "kind", "notes", "todo",
          "priority", "country")


def _user(name):
    with SessionLocal() as s:
        u = s.query(User).filter_by(username=name).one_or_none()
        if u is None:
            u = User(username=name, password_hash=authn.hash_password("pw"), auth_source="local")
            s.add(u)
            s.commit()
        s.refresh(u)
        s.expunge(u)
        return u


def _headers(user):
    with SessionLocal() as s:
        return {"Authorization": f"Bearer {authn.issue_access_token(s.get(User, user.id))}"}


def _clear():
    with SessionLocal() as s:
        s.query(PlaceListMember).delete()
        s.query(PlaceList).delete()
        s.query(SavedPlace).delete()
        s.commit()


def _row(**cells) -> str:
    """One CSV row in HEADER's order, from named cells, so no comma is miscounted.
    Quoted the way a spreadsheet writes it (a Maps link carries commas)."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow([cells.get(c, "") for c in HEADER])
    return buf.getvalue()


def _csv(*rows: str, header: str = ",".join(HEADER)) -> bytes:
    return ("\n".join((header, *rows)) + "\n").encode("utf-8")


def _import(user_id, data, dry_run=False):
    with SessionLocal() as s:
        return places_import.run(s, s.get(User, user_id), data, dry_run=dry_run)


def _places(user_id):
    with SessionLocal() as s:
        rows = s.query(SavedPlace).filter_by(owner_id=user_id).all()
        s.expunge_all()
        return {p.name: p for p in rows}


def _list_names(user_id):
    with SessionLocal() as s:
        return sorted(lst.name for lst, _ in places_svc.lists(s, s.get(User, user_id)))


@pytest.fixture
def world():
    """Ada has a list called Coffee; Ben has a list called Hikes and one place."""
    _clear()
    ada, ben = _user("imp-ada"), _user("imp-ben")
    with SessionLocal() as s:
        coffee = places_svc.create_list(s, ada, name="Coffee")
        hikes = places_svc.create_list(s, ben, name="Hikes")
        places_svc.save(s, ben, name="Example Ridge Hut", lat=46.1, lon=7.1, source="web")
        ids = {"coffee": coffee.id, "bens_list": hikes.id}
    yield {"ada": _headers(ada), "ben": _headers(ben), "ada_id": ada.id, "ben_id": ben.id,
           **ids}
    _clear()


THREE = _csv(
    _row(name="Example Lantern Cafe", lat=51.50321, lon=-0.11954, list="Coffee",
         kind="restaurant"),
    _row(name="Sample Mill Museum", list="Rainy days"),
    _row(name="Sample Mill Museum", list="coffee"))          # case-blind list match


def test_a_preview_writes_nothing_not_even_a_list(world):
    out = _import(world["ada_id"], THREE, dry_run=True)
    assert out["dry_run"] is True
    assert out["counts"] == {"created": 2, "duplicate": 1}     # the repeat is seen in the preview
    lists = {l["name"]: l for l in out["lists"]}
    assert lists["Rainy days"] == {"name": "Rainy days", "id": None, "outcome": "would_create"}
    assert lists["Coffee"]["outcome"] == "existing" and lists["Coffee"]["id"] == world["coffee"]
    assert _places(world["ada_id"]) == {}
    assert _list_names(world["ada_id"]) == ["Coffee"]


def test_an_import_sorts_into_lists_and_importing_again_adds_nothing(world):
    out = _import(world["ada_id"], THREE)
    assert out["counts"] == {"created": 2, "duplicate": 1}
    placed = _places(world["ada_id"])
    assert set(placed) == {"Example Lantern Cafe", "Sample Mill Museum"}
    assert placed["Example Lantern Cafe"].activity_type == "dining"
    assert placed["Example Lantern Cafe"].priority == "someday"
    mill = placed["Sample Mill Museum"]
    assert mill.lat is None and mill.lon is None                # no pin: waits in "Needs a pin"
    with SessionLocal() as s:
        on = places_svc.lists_for(s, [mill.id])[mill.id]
        names = sorted(s.get(PlaceList, i).name for i in on)
    assert names == ["Coffee", "Rainy days"]                    # a repeated row joins both
    again = _import(world["ada_id"], THREE)
    assert again["counts"] == {"duplicate": 3}
    assert {l["outcome"] for l in again["lists"]} == {"existing"}
    assert len(_places(world["ada_id"])) == 2
    assert _list_names(world["ada_id"]) == ["Coffee", "Rainy days"]


def test_an_import_never_changes_a_kind_already_chosen(world):
    with SessionLocal() as s:
        places_svc.save(s, s.get(User, world["ada_id"]), name="Example Lantern Cafe",
                        lat=51.50321, lon=-0.11954, source="web")
    _import(world["ada_id"], _csv(_row(name="Example Lantern Cafe", lat=51.50321,
                                       lon=-0.11954, kind="lodging")))
    assert _places(world["ada_id"])["Example Lantern Cafe"].activity_type != "lodging"


def test_zero_zero_and_half_pairs_are_no_pin(world):
    out = _import(world["ada_id"], _csv(
        _row(name="Example Nowhere Inn", lat=0, lon=0),
        _row(name="Sample Half Pier", lat=51.2)))
    assert out["counts"] == {"created": 2}
    for p in _places(world["ada_id"]).values():
        assert p.lat is None and p.lon is None


def test_fields_land_on_the_place(world):
    _import(world["ada_id"], _csv(_row(
        name="Sample Glasshouse", address="3 Example Road", lat=48.1, lon=11.5,
        notes="Opens at ten", todo="See the orchids", priority="MUST", country="de")))
    p = _places(world["ada_id"])["Sample Glasshouse"]
    assert (p.address, p.notes, p.todo, p.priority, p.country_code) == (
        "3 Example Road", "Opens at ten", "See the orchids", "must", "DE")
    assert p.source == "csv"


def test_a_byte_order_mark_and_any_header_order_and_case_work(world):
    data = b"\xef\xbb\xbf" + _csv("7.1,Example Lake Hut,46.2", header="LON,Name,Latitude")
    out = _import(world["ada_id"], data)
    assert out["counts"] == {"created": 1} and out["warnings"] == []
    p = _places(world["ada_id"])["Example Lake Hut"]
    assert (p.lat, p.lon) == (46.2, 7.1)


def test_semicolons_from_a_european_spreadsheet_work(world):
    out = _import(world["ada_id"], b"name;lat;lon\nExample Canal Bar;52.37;4.89\n")
    assert out["counts"] == {"created": 1}


def test_bad_rows_are_reported_and_never_stop_the_rest(world):
    out = _import(world["ada_id"], _csv(
        _row(name="Example Good Place", lat=51, lon=7),
        _row(name="Sample Bad Lat", lat="abc", lon=7),
        _row(name="Sample Off Map", lat=95, lon=7),
        _row(name="Sample Bad Kind", kind="spaceship"),
        _row(name="Sample Bad Priority", priority="soon"),
        _row(name="Sample Bad Country", country="GBR"),
        _row(notes="a row with no name and no url")))
    res = out["results"]
    assert [r["line"] for r in res] == list(range(2, 9))        # by line; the header is line 1
    assert res[0]["outcome"] == "created"
    for r in res[1:]:
        assert r["outcome"] == "error" and r["error"], r
    assert out["counts"] == {"created": 1, "error": 6}
    assert list(_places(world["ada_id"])) == ["Example Good Place"]


@pytest.mark.parametrize("data", [
    b"x" * (2 * 1024 * 1024 + 1),                    # over 2 MB
    "name\nCaf\xe9\n".encode("latin-1"),             # not UTF-8
    b"address,lat,lon\n3 Example Road,1,2\n",        # neither name nor url
    b"",
    b"name,lat,lon\n",                               # a header and no places
], ids=["too-big", "not-utf8", "no-name-column", "empty", "header-only"])
def test_a_file_it_cannot_use_is_refused_whole(world, data):
    with pytest.raises(places_import.UnreadableFile):
        _import(world["ada_id"], data, dry_run=True)


def test_too_many_rows_is_refused_whole(world, monkeypatch):
    monkeypatch.setattr(places_import, "MAX_ROWS", 3)
    with pytest.raises(places_import.UnreadableFile, match="more than 3"):
        _import(world["ada_id"], _csv(*(_row(name=f"Sample Stop {i}") for i in range(4))))
    assert _places(world["ada_id"]) == {}


def test_an_unknown_column_is_a_warning_and_ignored(world):
    out = _import(world["ada_id"], _csv("Example Kiosk,yes", header="name,Wheelchair"))
    assert out["counts"] == {"created": 1}
    assert len(out["warnings"]) == 1 and "Wheelchair" in out["warnings"][0]


def test_a_short_link_is_a_row_error_since_nothing_is_fetched(world):
    out = _import(world["ada_id"], _csv(_row(url="https://maps.app.goo.gl/exampleAbc123")))
    assert out["counts"] == {"error": 1}
    assert "short link" in out["results"][0]["error"]


def test_a_list_name_never_reaches_another_persons_list(world):
    out = _import(world["ada_id"], _csv(_row(name="Example Summit Trail", lat=46.3, lon=7.4,
                                             list="Hikes")))
    hikes = {l["name"]: l for l in out["lists"]}["Hikes"]
    assert hikes["outcome"] == "created" and hikes["id"] != world["bens_list"]
    with SessionLocal() as s:
        assert s.query(PlaceListMember).filter_by(list_id=world["bens_list"]).count() == 0
        assert s.get(PlaceList, hikes["id"]).owner_id == world["ada_id"]
    assert _places(world["ada_id"])["Example Summit Trail"].owner_id == world["ada_id"]
    bens = client.get("/api/v1/places", headers=world["ben"]).json()
    assert [p["name"] for p in bens] == ["Example Ridge Hut"]
    # Ben importing his own place matches his own copy, never anything of Ada's.
    out = _import(world["ben_id"], _csv(_row(name="Example Ridge Hut", lat=46.1, lon=7.1)))
    assert out["counts"] == {"duplicate": 1}


def test_the_api_takes_the_file(world):
    data = _csv(_row(name="Example Tram Stop Cafe", lat=51.1, lon=7.1))
    url = "/api/v1/places/import"
    r = client.post(url, files={"file": ("p.csv", data, "text/csv")},
                    data={"dry_run": "true"}, headers=world["ada"])
    assert r.status_code == 200 and r.json()["dry_run"] is True
    assert r.json()["results"][0] == {"line": 2, "outcome": "created", "id": None,
                                      "name": "Example Tram Stop Cafe", "list": None,
                                      "existing_status": None, "error": None}
    assert _places(world["ada_id"]) == {}
    assert client.post(url, files={"file": ("p.csv", data, "text/csv")}).status_code == 401
    bad = client.post(url, files={"file": ("p.csv", b"", "text/csv")}, headers=world["ada"])
    assert bad.status_code == 400 and "empty" in bad.json()["detail"]
    r = client.post(url, files={"file": ("p.csv", data, "text/csv")}, headers=world["ada"])
    assert r.json()["counts"] == {"created": 1}


class _As:
    """Call MCP tools as one person."""
    def __init__(self, user_id):
        self.user_id = user_id

    def __enter__(self):
        self.token = mcp_server._caller_id.set(self.user_id)
        return mcp_server

    def __exit__(self, *exc):
        mcp_server._caller_id.reset(self.token)


def test_the_mcp_tool_previews_by_default(world):
    text = _csv(_row(name="Example Ferry Terrace", lat=51.2, lon=7.2)).decode()
    with _As(world["ada_id"]) as m:
        assert m.import_places_csv(text)["dry_run"] is True
        assert _places(world["ada_id"]) == {}
        assert m.import_places_csv(text, dry_run=False)["counts"] == {"created": 1}
        with pytest.raises(mcp_server.ToolError, match="empty"):
            m.import_places_csv("")


def test_the_web_page_previews_then_imports_the_same_file(world):
    page = client.get("/places/import")
    assert page.status_code == 200 and "Download a template" in page.text
    tpl = client.get("/places/import/template.csv")
    assert tpl.status_code == 200 and tpl.headers["content-type"].startswith("text/csv")
    assert "error" not in _import(world["ada_id"], tpl.content, dry_run=True)["counts"]
    files = {"file": ("p.csv", THREE, "text/csv")}
    prev = client.post("/places/import", files=files, data={"action": "preview"})
    assert prev.status_code == 200 and "nothing has been saved" in prev.text
    assert "HX-Trigger" not in prev.headers
    done = client.post("/places/import", files=files, data={"action": "import"})
    assert "Imported 3 rows" in done.text and done.headers["HX-Trigger"] == "places-changed"
    none = client.post("/places/import", data={"action": "import"})
    assert "Choose a CSV file first" in none.text


GOOGLE_PLACE = ("https://www.google.com/maps/place/Example+Quay+Cafe/@51.5,-0.1,17z/"
                "data=!3m1!4b1!4m6!3m5!1s0x48761b3333333333:0x1234567890abcdef!8m2!3d51.5!4d-0.1")


def test_maps_links_are_read_offline_and_an_unreadable_one_never_costs_a_named_row(world):
    out = _import(world["ada_id"], _csv(
        _row(url=GOOGLE_PLACE),                                         # name and pin from the link
        _row(url="http://maps.google.com/?q=48.85661,2.35222", name="Sample Square"),
        _row(url="http://maps.google.com/?cid=1311768467294899695", name="Sample Cid Cafe"),
        _row(url="http://maps.google.com/?cid=1311768467294899695"),    # nothing else: refused
        _row(url="https://example.test/not-a-map", name="Sample Website Bar")))
    assert [r["outcome"] for r in out["results"]] == [
        "created", "created", "created", "error", "created"]
    placed = _places(world["ada_id"])
    quay = placed["Example Quay Cafe"]
    assert (quay.provider, quay.lat, quay.lon) == ("google", 51.5, -0.1)
    assert (placed["Sample Square"].lat, placed["Sample Square"].lon) == (48.85661, 2.35222)
    cid = placed["Sample Cid Cafe"]
    assert cid.lat is None and cid.source_url == "http://maps.google.com/?cid=1311768467294899695"
    assert placed["Sample Website Bar"].source_url is None             # only Maps links are kept
    again = _import(world["ada_id"], _csv(_row(url=GOOGLE_PLACE, list="Coffee")))
    assert again["counts"] == {"duplicate": 1}                          # same feature id, same place


def test_a_broken_csv_line_is_refused_without_the_parsers_own_message(world):
    data = b"name,notes\nExample Kiosk," + b"x" * 200_000 + b"\n"   # over csv's field limit
    r = client.post("/api/v1/places/import", files={"file": ("p.csv", data, "text/csv")},
                    headers=world["ada"])
    assert r.status_code == 400
    assert r.json()["detail"] == "line 2 can't be read as CSV; check its quotes"
