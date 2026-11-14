"""SPEC-13 §2 and §4 (D-241) — the Run·dere archive.

The format holds (every file of the demo account's archive validates against
schemas/archive/v1 and matches its manifest checksum); no credential is in an
archive; nothing of another person's beyond a name; export ids are own-only;
the pages are web-only, behind a fresh confirmation, gone on a demo server and
with sign-in off; builds are limited; and archives are deleted after 7 days."""
import io
import json
import hashlib
import stat
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    import jsonschema
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import archive, archive_jobs, authn, confirm, demo, demo_seed, retention
    from app import models as m
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app

    m.Base.metadata.create_all(engine)
    SCHEMAS = ROOT / "schemas" / "archive" / "v1"
    PW = "arx-correct-horse"

    # ------------------------------------------------------------------ fixtures
    @pytest.fixture(autouse=True)
    def env(tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        monkeypatch.setattr(settings, "auth_cookie_secure", False)
        monkeypatch.setattr(settings, "export_dir", str(tmp_path / "exports"))
        monkeypatch.setattr(settings, "documents_dir", str(tmp_path / "docs"))
        monkeypatch.setattr(settings, "public_base_url", "")
        # Builds run inline, so a test sees the result at once.
        monkeypatch.setattr(archive_jobs._EXECUTOR, "submit",
                            lambda fn, *a, **k: fn(*a, **k))
        confirm._attempts._hits.clear()
        yield tmp_path
        _cleanup()

    def _cleanup():
        with SessionLocal() as s:
            users = s.query(m.User).filter(m.User.username.like("arx-%")).all()
            uids = [u.id for u in users]
            tids = [u.traveler_id for u in users if u.traveler_id is not None]
            trips = [t.id for t in s.query(m.Trip).filter(m.Trip.name.like("Arx %")).all()]
            for model in (m.TripActivity, m.TripTraveler, m.TripMember, m.TripExpense,
                          m.TripBudgetItem, m.TripRate, m.TripExpenseComment,
                          m.DiscoveryFinding, m.DiscoveryItem, m.DiscoveryExclusion,
                          m.PackingPlan, m.PackingItem):
                s.query(model).filter(model.trip_id.in_(trips)).delete(synchronize_session=False)
            for model, col in ((m.ArchiveExport, "user_id"), (m.AccountEvent, "user_id"),
                               (m.JournalEntry, "owner_id"), (m.Document, "owner_id"),
                               (m.SavedPlace, "owner_id"), (m.PlaceList, "owner_id"),
                               (m.PlaceListMember, "owner_id"),
                               (m.SavedPlaceFiling, "owner_id"),
                               (m.InventoryItem, "user_id"), (m.Bag, "user_id"),
                               (m.PackingTemplate, "user_id"), (m.TravelItem, "owner_id"),
                               (m.RefreshToken, "user_id"),
                               (m.PasswordResetToken, "user_id"),
                               (m.OAuthCode, "user_id"), (m.OAuthGrant, "user_id"),
                               (m.PersonalApiToken, "user_id"), (m.ApiKey, "user_id"),
                               (m.UserIntegration, "user_id"), (m.InboxAddress, "user_id")):
                s.query(model).filter(getattr(model, col).in_(uids)).delete(
                    synchronize_session=False)
            for model in (m.HomeProfile, m.PackingProfile, m.DiscoveryProfile):
                s.query(model).filter(model.traveler_id.in_(tids)).delete(
                    synchronize_session=False)
            s.query(m.TripExpenseShare).filter(
                m.TripExpenseShare.traveler_id.in_(tids)).delete(synchronize_session=False)
            s.query(m.TripBudgetTraveler).filter(
                m.TripBudgetTraveler.traveler_id.in_(tids)).delete(synchronize_session=False)
            s.query(m.TravelerIdentity).filter(m.TravelerIdentity.id.in_(tids)).delete(
                synchronize_session=False)
            s.query(m.Trip).filter(m.Trip.id.in_(trips)).delete(synchronize_session=False)
            s.query(m.User).filter(m.User.id.in_(uids)).delete(synchronize_session=False)
            s.commit()

    def _user(name, *, person=None, source="local", admin=False, email=True, **kw):
        """An account `arx-<name>` with its own traveller."""
        with SessionLocal() as s:
            t = m.TravelerIdentity(display_name=person or f"Arx {name.title()}",
                                   nationality="NZ", base_currency="NZD")
            s.add(t)
            s.flush()
            u = m.User(username=f"arx-{name}", auth_source=source, is_admin=admin,
                       email=f"{name}@example.test" if email else None,
                       password_hash=authn.hash_password(PW) if source == "local" else None,
                       traveler_id=t.id, **kw)
            s.add(u)
            s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _web(user):
        """A browser signed in as `user` (a web session cookie)."""
        c = TestClient(app, follow_redirects=False)
        c.cookies.set(settings.auth_cookie_name, authn.issue_web_token(user))
        return c

    def _confirm(c):
        r = c.post("/account/confirm", data={"password": PW, "next": "/account/export"})
        assert r.status_code == 303, r.text
        return c

    def _build(c):
        r = c.post("/account/export/build")
        assert r.status_code == 303 and r.headers["location"] == "/account/export", r.headers
        return _latest(c)

    def _latest(c_or_uid):
        with SessionLocal() as s:
            q = s.query(m.ArchiveExport).order_by(m.ArchiveExport.requested_at.desc())
            return q.first()

    def _zip_for(user) -> zipfile.ZipFile:
        """Build `user`'s archive straight to memory."""
        tmp = Path(settings.export_dir).parent / f"direct-{user.id}.zip"
        with SessionLocal() as s:
            archive.build(s, s.get(m.User, user.id), tmp)
        return zipfile.ZipFile(io.BytesIO(tmp.read_bytes()))

    def _all_bytes(zf: zipfile.ZipFile) -> bytes:
        return b"\n".join(zf.read(n) for n in zf.namelist()) + "\n".join(zf.namelist()).encode()

    def _json(zf, name):
        return json.loads(zf.read(name))

    # ------------------------------------------------------------------ format (§2.2, §2.3)
    def _validate(zf: zipfile.ZipFile) -> dict:
        manifest = _json(zf, "manifest.json")
        jsonschema.validate(manifest, json.loads((SCHEMAS / "manifest.schema.json").read_text()))
        listed = {f["path"] for f in manifest["files"]}
        assert listed | {"manifest.json"} == set(zf.namelist())
        for f in manifest["files"]:
            data = zf.read(f["path"])
            assert hashlib.sha256(data).hexdigest() == f["sha256"], f["path"]
            assert len(data) == f["bytes"], f["path"]
            if f["section"] in ("readme", "document"):
                continue
            schema = json.loads((SCHEMAS / f"{f['section']}.schema.json").read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.validate(json.loads(data), schema)
        return manifest

    def test_every_schema_is_strict():
        """`additionalProperties: false` on every object a schema defines (§2.3)."""
        names = {p.name for p in SCHEMAS.glob("*.schema.json")}
        assert names == {f"{n}.schema.json" for n in (
            "manifest", "account", "traveller", "discovery", "packing", "trip", "places",
            "journal", "documents")}

        def walk(node, path):
            if isinstance(node, dict):
                if "properties" in node:
                    assert node.get("additionalProperties") is False, path
                    assert set(node["required"]) == set(node["properties"]), path
                for k, v in node.items():
                    walk(v, f"{path}/{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{path}/{i}")
        for p in SCHEMAS.glob("*.schema.json"):
            walk(json.loads(p.read_text()), p.name)

    def test_the_demo_accounts_archive_conforms_to_the_schemas(monkeypatch):
        """CI's hold on the format (§2.3): export the demo seed's account and
        validate every file and every checksum."""
        for k, v in (("demo_mode", True), ("demo_username", "arx-demo"),
                     ("demo_password", "pw")):
            monkeypatch.setattr(settings, k, v)
        with SessionLocal() as s:
            from planning.catalog import seed_catalog
            seed_catalog(s)
            demo.ensure_demo_user(s)
            demo_seed.seed(s, replace=True, today=date.today())
            user = demo_seed._demo_user(s)
            s.expunge(user)
        try:
            zf = _zip_for(user)
            manifest = _validate(zf)
            assert (manifest["format"], manifest["format_version"]) == ("rundere-archive", 1)
            assert manifest["username"] == "arx-demo"
            assert manifest["server_version"] == settings.app_version
            sections = [f["section"] for f in manifest["files"]]
            assert sections.count("trip") == 3
            trips = [_json(zf, f["path"]) for f in manifest["files"] if f["section"] == "trip"]
            assert all(t["my_role"] == "owner" and not t["shared_with_me"] for t in trips)
            assert any(t["discover"]["findings"] for t in trips)
            assert any(t["expenses"] for t in trips) and any(t["packing"] for t in trips)
            assert any(t["timeline"] for t in trips)
            me = [p for t in trips for p in t["travellers"] if p["is_me"]]
            assert me and {p["name"] for p in me} == {"Jane Doe"}
            assert len(_json(zf, "places.json")["places"]) >= 13
            assert _json(zf, "journal.json")["entries"]
            assert _json(zf, "profile/traveller.json")["traveller"]["name"] == "Jane Doe"
            assert "README.txt" in zf.namelist()
        finally:
            with SessionLocal() as s:
                demo_seed._remove(s, s.get(m.User, user.id))
                s.query(m.TravelerIdentity).filter_by(display_name="Jane Doe").delete()
                s.commit()

    def test_json_conventions(env):
        ana = _user("ana")
        with SessionLocal() as s:
            s.add(m.JournalEntry(owner_id=ana.id, entry_date="2031-03-04", body="Kelp day",
                                 captured_at=datetime(2031, 3, 4, 9, 30, tzinfo=timezone.utc),
                                 place_label=""))
            s.commit()
        zf = _zip_for(ana)
        _validate(zf)
        entry = _json(zf, "journal.json")["entries"][0]
        assert entry["captured_at"] == "2031-03-04T09:30:00Z"         # UTC, ends in Z
        assert entry["place"] is None and entry["trip_id"] is None    # null, never ""
        assert zf.read("manifest.json").decode("utf-8")               # UTF-8

    def test_document_paths_are_generated_never_the_uploaded_name(env):
        ana = _user("ana")
        data = b"%PDF-1.4 arx-file-body"
        sha = hashlib.sha256(data).hexdigest()
        folder = Path(settings.documents_dir) / ana.id
        folder.mkdir(parents=True)
        (folder / sha).write_bytes(data)
        with SessionLocal() as s:
            s.add(m.Document(owner_id=ana.id, sha256=sha, content_type="application/pdf",
                             size_bytes=len(data), filename="../../etc/arx-evil.pdf"))
            s.add(m.Document(owner_id=ana.id, sha256="b" * 64, content_type="text/calendar",
                             size_bytes=5, filename="gone.ics",
                             purged_at=datetime(2030, 1, 1, tzinfo=timezone.utc)))
            s.commit()
        zf = _zip_for(ana)
        _validate(zf)
        assert f"documents/{sha}.pdf" in zf.namelist()
        assert zf.read(f"documents/{sha}.pdf") == data
        assert not any(".." in n or n.startswith("/") or "evil" in n for n in zf.namelist())
        docs = {d["sha256"]: d for d in _json(zf, "documents/index.json")["documents"]}
        assert docs[sha]["filename"] == "../../etc/arx-evil.pdf"       # a field, not a path
        assert docs[sha]["path"] == f"documents/{sha}.pdf" and docs[sha]["remove_on"]
        gone = docs["b" * 64]
        assert gone["path"] is None and gone["removed_at"] == "2030-01-01T00:00:00Z"

    # ------------------------------------------------------------------ credentials (§4.1)
    def test_no_credential_is_in_an_archive(env):
        """An account with every kind of credential: no stored hash, ciphertext,
        hint or token appears anywhere in the archive's bytes."""
        ana = _user("ana", external_subject="oidc:arx-subject-9f3")
        later = datetime.now(timezone.utc) + timedelta(days=1)
        secrets = {
            "refresh": "a1" * 32, "reset": "b2" * 32, "code": "c3" * 32, "grant": "d4" * 32,
            "grant_prev": "e5" * 32, "pat": "f6" * 32, "key": "a7" * 32,
            "ct": "v1:arx-ciphertext-Zm9vYmFy", "hint": "…x9Qz", "inbox": "arxinboxtoken77",
        }
        with SessionLocal() as s:
            s.add_all([
                m.RefreshToken(user_id=ana.id, token_hash=secrets["refresh"], expires_at=later),
                m.PasswordResetToken(user_id=ana.id, token_hash=secrets["reset"],
                                     expires_at=later),
                m.OAuthCode(user_id=ana.id, code_hash=secrets["code"], client_id="arx-client",
                            redirect_uri="https://client.example.test/cb",
                            code_challenge="arx-challenge", expires_at=later),
                m.OAuthGrant(user_id=ana.id, client_id="arx-client",
                             refresh_hash=secrets["grant"],
                             prev_refresh_hashes=[secrets["grant_prev"]], expires_at=later),
                m.PersonalApiToken(user_id=ana.id, name="arx-laptop-token",
                                   token_hash=secrets["pat"]),
                m.ApiKey(user_id=ana.id, client_name="arx-ingest-client",
                         key_hash=secrets["key"]),
                m.UserIntegration(user_id=ana.id, kind="airtrail",
                                  base_url="https://airtrail.example.test",
                                  credential_ct=secrets["ct"], credential_hint=secrets["hint"],
                                  remote_user_id="arx-remote-42"),
                m.InboxAddress(user_id=ana.id, token=secrets["inbox"]),
            ])
            s.commit()
            row = s.get(m.User, ana.id)
            password_hash, version = row.password_hash, row.token_version
        zf = _zip_for(ana)
        _validate(zf)
        blob = _all_bytes(zf)
        for name, value in secrets.items():
            assert value.encode() not in blob, name
        assert password_hash.encode() not in blob
        assert password_hash.split("$")[-1].encode() not in blob
        for other in ("arx-subject-9f3", "oidc:", "arx-client", "arx-laptop-token",
                      "arx-ingest-client", "airtrail.example.test", "arx-remote-42",
                      "arx-challenge", "client.example.test", "token_version",
                      "password_hash", "external_subject", "credential"):
            assert other.encode() not in blob, other
        account = _json(zf, "profile/account.json")
        assert set(account) == {"username", "email", "sign_in", "created_at", "is_admin",
                                "support_access", "account_log"}
        assert account["sign_in"] == "local" and version == 0

    # ------------------------------------------------------------------ other people (§4.1)
    def _two_people():
        """Ana and Ben, each owning a trip the other travels on, each with a
        full set of private data on both."""
        ana, ben = _user("ana"), _user("ben", person="Arx Benedikt")
        with SessionLocal() as s:
            mine = m.Trip(name="Arx Coast", owner_id=ana.id, start_date="2031-05-01",
                          end_date="2031-05-09", notes="Arx coast notes",
                          expense_currency="NZD")
            theirs = m.Trip(name="Arx Hills", owner_id=ben.id, start_date="2031-07-01",
                            end_date="2031-07-05", notes="BEN-TRIP-NOTES",
                            expense_currency="NZD")
            s.add_all([mine, theirs])
            s.flush()
            for trip in (mine, theirs):
                s.add_all([m.TripTraveler(trip_id=trip.id, traveler_id=ana.traveler_id),
                           m.TripTraveler(trip_id=trip.id, traveler_id=ben.traveler_id)])
                for who, tag in ((ana, "ANA"), (ben, "BEN")):
                    s.add(m.JournalEntry(owner_id=who.id, entry_date=trip.start_date,
                                         trip_id=trip.id, body=f"{tag}-JOURNAL-{trip.name}"))
                    f = m.DiscoveryFinding(trip_id=trip.id, user_id=who.id, topic="food",
                                           scope="city", place_key="NZ:Kaikoura",
                                           body_md=f"{tag}-DISCOVER-{trip.name}")
                    s.add(f)
                    s.flush()
                    s.add(m.DiscoveryItem(finding_id=f.id, trip_id=trip.id, user_id=who.id,
                                          topic="food", place_key="NZ:Kaikoura", kind="cafe",
                                          name=f"{tag}-CAFE-{trip.name}"))
                    s.add(m.DiscoveryExclusion(trip_id=trip.id, user_id=who.id,
                                               place_key=f"NZ:{tag}-SKIPPED"))
                    plan = m.PackingPlan(trip_id=trip.id, user_id=who.id, status="done")
                    s.add(plan)
                    s.flush()
                    s.add(m.PackingItem(plan_id=plan.id, trip_id=trip.id, user_id=who.id,
                                        item_key=f"misc:{tag.lower()}", category="misc",
                                        label=f"{tag}-PACKED-{trip.name}"))
                    s.add(m.Document(owner_id=who.id,
                                     sha256=hashlib.sha256(
                                         f"{tag}{trip.name}".encode()).hexdigest(),
                                     content_type="application/pdf", size_bytes=9,
                                     filename=f"{tag}-FILE-{trip.name}.pdf", trip_id=trip.id))
                    # A planned cost for themselves alone: hidden from the other (D-183).
                    b = m.TripBudgetItem(trip_id=trip.id, owner_id=who.id,
                                         title=f"{tag}-PLANNED-{trip.name}", amount="120.00",
                                         currency="NZD")
                    s.add(b)
                    s.flush()
                    s.add(m.TripBudgetTraveler(budget_item_id=b.id, traveler_id=who.traveler_id))
                    # An expense they entered, split between both.
                    e = m.TripExpense(trip_id=trip.id, owner_id=who.id,
                                      title=f"{tag}-SHARED-EXPENSE-{trip.name}",
                                      amount="60.00", currency="NZD", payer_id=who.traveler_id)
                    s.add(e)
                    s.flush()
                    for t in (ana, ben):
                        s.add(m.TripExpenseShare(expense_id=e.id, traveler_id=t.traveler_id,
                                                 amount="30.00"))
                    s.add(m.TripExpenseComment(expense_id=e.id, trip_id=trip.id,
                                               author_id=who.id, body=f"{tag}-COMMENT"))
                    # …and one for themselves alone, which the other can't see.
                    solo = m.TripExpense(trip_id=trip.id, owner_id=who.id,
                                         title=f"{tag}-SOLO-EXPENSE-{trip.name}",
                                         amount="15.00", currency="NZD",
                                         payer_id=who.traveler_id)
                    s.add(solo)
                    s.flush()
                    s.add(m.TripExpenseShare(expense_id=solo.id, traveler_id=who.traveler_id,
                                             amount="15.00"))
                    # A timeline item logged from their own capture.
                    key = f"flight:{tag}:{trip.id}"
                    s.add(m.TravelItem(owner_id=who.id, trip_id=trip.id, canonical_key=key,
                                       item_type="flight"))
                    s.add(m.TripActivity(trip_id=trip.id, type="flight",
                                         starts_at=f"{trip.start_date}T08:00",
                                         source_item_key=key,
                                         values={"title": f"{tag}-CAPTURED-{trip.name}"}))
                s.add(m.TripActivity(trip_id=trip.id, type="note", starts_at="",
                                     values={"title": f"HAND-MADE-{trip.name}"}))
                s.add(m.TripRate(trip_id=trip.id, currency="AUD", quote="NZD",
                                 rate_date=trip.start_date, rate="1.08", source="frankfurter",
                                 fetched_by=ben.id))
            s.add(m.SavedPlace(owner_id=ben.id, place_key="other:ben-1", name="BEN-PLACE"))
            s.add(m.InventoryItem(user_id=ben.id, label="BEN-INVENTORY", category="misc"))
            s.add(m.Bag(user_id=ben.id, label="BEN-BAG"))
            s.add(m.HomeProfile(traveler_id=ben.traveler_id, kind="residence",
                                label="BEN-HOME"))
            s.add(m.TripMember(trip_id=mine.id, user_id=ben.id))
            s.commit()
            return ana, ben, mine.id, theirs.id

    def test_another_persons_data_is_absent(env):
        ana, ben, mine, theirs = _two_people()
        zf = _zip_for(ana)
        _validate(zf)
        blob = _all_bytes(zf)
        # Ben's journal, files, Discover, packing, hidden costs, email and ids.
        for needle in ("BEN-JOURNAL", "BEN-FILE", "BEN-DISCOVER", "BEN-CAFE", "BEN-SKIPPED",
                       "BEN-PACKED", "BEN-PLANNED", "BEN-SOLO-EXPENSE", "BEN-PLACE",
                       "BEN-INVENTORY", "BEN-BAG", "BEN-HOME", "BEN-COMMENT",
                       "ben@example.test", "arx-ben", ben.id, "fetched_by", "owner_id",
                       "user_id", "traveler_id", "payer_id"):
            assert needle.encode() not in blob, needle
        # …while his name is there, as a name.
        own = _json(zf, f"trips/{mine}.json")
        assert {(p["name"], p["is_me"]) for p in own["travellers"]} == {
            ("Arx Ana", True), ("Arx Benedikt", False)}
        assert set(own["travellers"][0]) == {"ref", "name", "is_me"}

        # The trip Ana owns, in full as far as she can see it.
        titles = {a["values"]["title"] for a in own["timeline"]}
        assert titles == {"ANA-CAPTURED-Arx Coast", "BEN-CAPTURED-Arx Coast",
                          "HAND-MADE-Arx Coast"}
        assert own["shared_with_me"] is False and own["notes"] == "Arx coast notes"
        assert {e["title"] for e in own["expenses"]} == {
            "ANA-SHARED-EXPENSE-Arx Coast", "ANA-SOLO-EXPENSE-Arx Coast",
            "BEN-SHARED-EXPENSE-Arx Coast"}
        assert [b["title"] for b in own["planned_costs"]] == ["ANA-PLANNED-Arx Coast"]
        assert [c["body"] for c in own["my_comments"]] == ["ANA-COMMENT"]
        assert len(own["rates"]) == 1 and "fetched_by" not in own["rates"][0]
        assert [f["body_md"] for f in own["discover"]["findings"]] == ["ANA-DISCOVER-Arx Coast"]
        assert own["discover"]["excluded_places"] == ["NZ:ANA-SKIPPED"]
        assert [p["label"] for p in own["packing"]["items"]] == ["ANA-PACKED-Arx Coast"]
        me = next(p["ref"] for p in own["travellers"] if p["is_me"])
        shared_expense = next(e for e in own["expenses"] if "SHARED" in e["title"]
                              and e["entered_by_me"])
        assert shared_expense["payer"] == me
        assert {s["traveller"] for s in shared_expense["shares"]} == {
            p["ref"] for p in own["travellers"]}

        # The trip shared with her: a header and only what she added.
        other = _json(zf, f"trips/{theirs}.json")
        assert other["shared_with_me"] is True and other["my_role"] == "traveller"
        assert (other["name"], other["start_date"]) == ("Arx Hills", "2031-07-01")
        assert other["notes"] is None and other["rates"] == []
        assert b"BEN-TRIP-NOTES" not in blob
        assert [a["values"]["title"] for a in other["timeline"]] == ["ANA-CAPTURED-Arx Hills"]
        assert b"HAND-MADE-Arx Hills" not in blob and b"BEN-CAPTURED-Arx Hills" not in blob
        assert {e["title"] for e in other["expenses"]} == {
            "ANA-SHARED-EXPENSE-Arx Hills", "ANA-SOLO-EXPENSE-Arx Hills"}
        assert all(e["entered_by_me"] for e in other["expenses"])
        assert b"BEN-SHARED-EXPENSE-Arx Hills" not in blob
        assert [b["title"] for b in other["planned_costs"]] == ["ANA-PLANNED-Arx Hills"]
        assert [f["body_md"] for f in other["discover"]["findings"]] == ["ANA-DISCOVER-Arx Hills"]
        # Her own journal and files follow her onto his trip.
        bodies = {e["body"] for e in _json(zf, "journal.json")["entries"]}
        assert bodies == {"ANA-JOURNAL-Arx Coast", "ANA-JOURNAL-Arx Hills"}
        names = {d["filename"] for d in _json(zf, "documents/index.json")["documents"]}
        assert names == {"ANA-FILE-Arx Coast.pdf", "ANA-FILE-Arx Hills.pdf"}

    def test_a_view_only_share_gets_a_header_and_nothing_more(env):
        ana, ben, mine, theirs = _two_people()     # Ben also has a share row on Ana's trip
        with SessionLocal() as s:
            s.query(m.TripTraveler).filter_by(trip_id=mine, traveler_id=ben.traveler_id).delete()
            s.commit()
        zf = _zip_for(ben)
        _validate(zf)
        doc = _json(zf, f"trips/{mine}.json")
        assert doc["my_role"] == "viewer" and doc["shared_with_me"] is True
        assert doc["name"] == "Arx Coast" and doc["notes"] is None

    def test_admin_and_support_access_never_widen_an_archive(env):
        """Ownerless rows belong to nobody, and what an admin sees under support
        access (D-211) is the other person's, not theirs."""
        boss = _user("boss", admin=True)
        cara = _user("cara", share_with_admins=True)
        with SessionLocal() as s:
            s.add_all([m.Trip(name="Arx Cara Private", owner_id=cara.id),
                       m.Trip(name="Arx Nobody", owner_id=None),
                       m.JournalEntry(owner_id=None, entry_date="2031-01-01",
                                      body="OWNERLESS-JOURNAL"),
                       m.Bag(user_id=None, label="OWNERLESS-BAG"),
                       m.InventoryItem(user_id=None, label="OWNERLESS-ITEM", category="misc")])
            s.commit()
        try:
            zf = _zip_for(boss)
            _validate(zf)
            blob = _all_bytes(zf)
            for needle in ("Arx Cara Private", "Arx Nobody", "OWNERLESS-JOURNAL",
                           "OWNERLESS-BAG", "OWNERLESS-ITEM", cara.id):
                assert needle.encode() not in blob, needle
            assert not [n for n in zf.namelist() if n.startswith("trips/")]
        finally:
            with SessionLocal() as s:
                s.query(m.JournalEntry).filter_by(body="OWNERLESS-JOURNAL").delete()
                s.query(m.Bag).filter_by(label="OWNERLESS-BAG").delete()
                s.query(m.InventoryItem).filter_by(label="OWNERLESS-ITEM").delete()
                s.commit()

    def test_the_account_log_says_who_without_naming_them(env):
        ana, boss = _user("ana"), _user("boss", admin=True)
        with SessionLocal() as s:
            s.add_all([
                m.AccountEvent(user_id=ana.id, actor_id=ana.id, action="support_on"),
                m.AccountEvent(user_id=ana.id, actor_id=boss.id, action="admin_on",
                               detail="ARX-DETAIL"),
                m.AccountEvent(user_id=ana.id, actor_id=None, action="password_set"),
                m.AccountEvent(user_id=boss.id, actor_id=boss.id, action="BOSS-OWN-EVENT")])
            s.commit()
        zf = _zip_for(ana)
        log = _json(zf, "profile/account.json")["account_log"]
        assert {(e["action"], e["by"]) for e in log} == {
            ("support_on", "you"), ("admin_on", "an admin"), ("password_set", "the server")}
        blob = _all_bytes(zf)
        assert boss.id.encode() not in blob and b"ARX-DETAIL" not in blob
        assert b"BOSS-OWN-EVENT" not in blob

    # ------------------------------------------------------------------ build + download (§2.5)
    def test_build_and_download(env):
        ana = _user("ana")
        c = _web(ana)
        page = c.get("/account/export")
        assert page.status_code == 200 and "Build my archive" in page.text
        row = _build(c)
        assert row.status == "ready" and row.user_id == ana.id and row.format_version == 1
        path = Path(settings.export_dir) / ana.id / f"{row.id}.zip"
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600           # the server's user only
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert row.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert row.size_bytes == path.stat().st_size
        expires = row.expires_at.replace(tzinfo=timezone.utc)
        assert timedelta(days=6, hours=23) < expires - datetime.now(timezone.utc) <= timedelta(days=7)

        # A session alone isn't enough: the download asks to confirm first.
        r = c.get(f"/account/export/{row.id}/download")
        assert r.status_code == 303
        assert r.headers["location"] == "/account/confirm?next=%2Faccount%2Fexport"
        assert "Confirm it's me" in c.get("/account/export").text
        assert c.post("/account/confirm", data={"password": "wrong",
                                                "next": "/account/export"}).status_code == 400
        assert c.get(f"/account/export/{row.id}/download").status_code == 303
        _confirm(c)
        r = c.get(f"/account/export/{row.id}/download")
        assert r.status_code == 200 and r.content == path.read_bytes()
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert r.headers["content-disposition"] == (
            f'attachment; filename="rundere-archive-arx-ana-{today}.zip"')
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-store"
        _validate(zipfile.ZipFile(io.BytesIO(r.content)))
        # The account log records the build and the download.
        with SessionLocal() as s:
            actions = [e.action for e in s.query(m.AccountEvent).filter_by(user_id=ana.id)]
        assert sorted(actions) == ["export_built", "export_downloaded"]

    def test_a_new_build_replaces_the_old_archive(env):
        ana = _user("ana")
        c = _web(ana)
        first = _build(c)
        old = Path(settings.export_dir) / ana.id / f"{first.id}.zip"
        assert old.is_file()
        second = _build(c)
        assert second.id != first.id
        assert not old.exists()
        assert [p.name for p in old.parent.iterdir()] == [f"{second.id}.zip"]
        with SessionLocal() as s:
            assert s.get(m.ArchiveExport, first.id).status == "replaced"
        _confirm(c)
        assert c.get(f"/account/export/{first.id}/download").status_code == 404

    def test_someone_elses_export_id_is_a_404(env):
        ana, ben, boss = _user("ana"), _user("ben"), _user("boss", admin=True)
        row = _build(_web(ana))
        for other in (ben, boss):                 # an admin has no export of anyone else
            c = _confirm(_web(other))
            assert c.get(f"/account/export/{row.id}/download").status_code == 404
        assert _confirm(_web(ben)).get("/account/export/no-such-id/download").status_code == 404

    def test_tokens_cant_start_or_download_an_export(env):
        """No API, OAuth or personal-token path (D-241): web session only."""
        ana = _user("ana")
        row = _build(_web(ana))
        bearer = {"Authorization": f"Bearer {authn.issue_access_token(ana)}"}
        plain = TestClient(app, follow_redirects=False)
        assert plain.post("/account/export/build", headers=bearer).status_code == 403
        assert plain.get(f"/account/export/{row.id}/download", headers=bearer).status_code == 403
        assert plain.get("/account/export", headers=bearer).status_code == 403
        # The app's own token in the cookie's place isn't a web session either.
        c = TestClient(app, follow_redirects=False)
        c.cookies.set(settings.auth_cookie_name, authn.issue_access_token(ana))
        assert c.post("/account/export/build").status_code == 401
        # Signed out: the gate sends a browser to sign in.
        assert plain.get("/account/export").status_code == 303
        # And nothing under /api/v1 or MCP knows about exports.
        paths = [getattr(r, "path", "") for r in app.routes]
        assert not [p for p in paths if "export" in p and p.startswith("/api/")]
        import app.mcp_server as mcp_server
        source = Path(mcp_server.__file__).read_text()
        assert "archive" not in source and "export_data" not in source

    def test_no_export_with_sign_in_off(env, monkeypatch):
        monkeypatch.setattr(settings, "auth_required", False)
        c = TestClient(app, follow_redirects=False)
        assert c.get("/account/export").status_code == 404
        assert c.post("/account/export/build").status_code == 404
        assert c.get("/account/export/x/download").status_code == 404
        assert c.get("/account/confirm").status_code == 404

    def test_no_export_on_a_demo_server(env, monkeypatch):
        ana = _user("ana")
        row = _build(_web(ana))
        monkeypatch.setattr(settings, "demo_mode", True)
        c = _web(ana)
        for method, path in (("get", "/account/export"), ("post", "/account/export/build"),
                             ("get", f"/account/export/{row.id}/download"),
                             ("get", "/account/export/status"), ("get", "/account/confirm")):
            assert getattr(c, method)(path).status_code == 404, path
        assert "Export my data" not in c.get("/account").text

    def test_the_account_page_links_to_the_export(env):
        assert 'href="/account/export"' in _web(_user("ana")).get("/account").text

    # ------------------------------------------------------------------ limits (§2.5, §4.1)
    def test_three_builds_a_day(env):
        ana = _user("ana")
        c = _web(ana)
        for _ in range(3):
            _build(c)
        r = c.post("/account/export/build")
        assert r.status_code == 303 and "3+times+a+day" in r.headers["location"]
        with SessionLocal() as s:
            assert s.query(m.ArchiveExport).filter_by(user_id=ana.id).count() == 3
            # Yesterday's builds no longer count.
            for row in s.query(m.ArchiveExport).filter_by(user_id=ana.id):
                row.requested_at = datetime.now(timezone.utc) - timedelta(hours=25)
            s.commit()
        _build(c)

    def test_one_build_per_person_at_a_time(env, monkeypatch):
        ana, ben = _user("ana"), _user("ben")
        monkeypatch.setattr(archive_jobs._EXECUTOR, "submit", lambda fn, *a, **k: None)
        c = _web(ana)
        assert c.post("/account/export/build").headers["location"] == "/account/export"
        with SessionLocal() as s:
            running = s.query(m.ArchiveExport).filter_by(user_id=ana.id).one()
            assert running.status == "building"
        assert "Building your archive" in c.get("/account/export").text
        assert "hx-trigger" in c.get("/account/export/status").text
        r = c.post("/account/export/build")
        assert "already+being+built" in r.headers["location"]
        # Someone else isn't held up by it; the single worker queues them.
        assert _web(ben).post("/account/export/build").headers["location"] == "/account/export"
        assert archive_jobs._EXECUTOR._max_workers == 1        # one build on the server at a time
        with archive_jobs._LOCK:
            archive_jobs._LIVE.clear()

    def test_a_build_orphaned_by_a_restart_is_marked_failed(env):
        ana = _user("ana")
        with SessionLocal() as s:
            s.add(m.ArchiveExport(user_id=ana.id, status="building"))
            s.commit()
        c = _web(ana)
        assert "didn't finish" in c.get("/account/export").text
        with SessionLocal() as s:
            row = s.query(m.ArchiveExport).filter_by(user_id=ana.id).one()
            assert (row.status, row.error) == ("failed", "Interrupted")
        _build(c)                                 # and it doesn't block the next one

    def test_a_failed_build_keeps_only_the_error_class(env, monkeypatch):
        ana = _user("ana")

        def boom(*a, **k):
            raise RuntimeError("ARX-SECRET-IN-MESSAGE")
        monkeypatch.setattr(archive, "build", boom)
        c = _web(ana)
        c.post("/account/export/build")
        with SessionLocal() as s:
            row = s.query(m.ArchiveExport).filter_by(user_id=ana.id).one()
            assert (row.status, row.error) == ("failed", "RuntimeError")
        assert list((Path(settings.export_dir) / ana.id).iterdir()) == []

    def test_no_build_without_free_space(env, monkeypatch):
        ana = _user("ana")
        with SessionLocal() as s:
            s.add(m.Document(owner_id=ana.id, sha256="c" * 64, content_type="application/pdf",
                             size_bytes=50 * 1024 * 1024, filename="big.pdf"))
            s.commit()
        need = 50 * 1024 * 1024 + archive_jobs.SPACE_MARGIN
        monkeypatch.setattr(archive_jobs, "_free_bytes", lambda: need - 1)
        c = _web(ana)
        r = c.post("/account/export/build")
        assert "enough+free+space" in r.headers["location"]
        with SessionLocal() as s:
            assert s.query(m.ArchiveExport).filter_by(user_id=ana.id).count() == 0
            assert s.query(m.Document).filter_by(owner_id=ana.id).count() == 1   # nothing deleted
        monkeypatch.setattr(archive_jobs, "_free_bytes", lambda: need)
        _build(c)

    # ------------------------------------------------------------------ email (§2.5)
    def test_the_ready_email_links_to_the_account_page_never_the_file(env, monkeypatch):
        from app import email
        sent = []
        monkeypatch.setattr(email, "email_configured", lambda: True)
        monkeypatch.setattr(email, "send_email",
                            lambda to, subject, body: sent.append((to, subject, body)) or True)
        monkeypatch.setattr(settings, "public_base_url", "https://rundere.example.test")
        ana, ben = _user("ana"), _user("ben", email=False)
        row = _build(_web(ana))
        _build(_web(ben))                         # no address: no email
        assert [to for to, _, _ in sent] == ["ana@example.test"]
        body = sent[0][2]
        assert "https://rundere.example.test/account/export" in body
        assert row.id not in body and "download" not in body.split("account/export")[1][:12]
        assert ".zip" not in body

    def test_no_email_without_smtp(env, monkeypatch):
        from app import email
        monkeypatch.setattr(email, "deliver",
                            lambda *a, **k: pytest.fail("no SMTP is configured"))
        assert _build(_web(_user("ana"))).status == "ready"

    # ------------------------------------------------------------------ retention (§2.5)
    def test_archives_are_deleted_after_seven_days(env):
        ana = _user("ana")
        c = _web(ana)
        row = _build(c)
        path = Path(settings.export_dir) / ana.id / f"{row.id}.zip"
        day = lambda n: date.today() + timedelta(days=n)
        with SessionLocal() as s:
            assert archive_jobs.expire_due(s) == 0
            ahead = datetime.now(timezone.utc) + timedelta(days=7, minutes=1)
            assert archive_jobs.expire_due(s, now=ahead, dry_run=True) == 1
        assert path.is_file()
        with SessionLocal() as s:                 # make it due, then run the daily job
            s.get(m.ArchiveExport, row.id).expires_at = (
                datetime.now(timezone.utc) - timedelta(minutes=1))
            s.commit()
        report = retention.run(SessionLocal, today=day(0), mailbox=False)
        assert report.exports_expired == 1 and report.summary()["exports_expired"] == 1
        assert not path.exists()
        with SessionLocal() as s:
            assert s.get(m.ArchiveExport, row.id).status == "expired"
        assert retention.run(SessionLocal, today=day(0), mailbox=False).exports_expired == 0
        _confirm(c)
        assert c.get(f"/account/export/{row.id}/download").status_code == 404
        assert "deleted after 7 days" in c.get("/account/export").text

    # ------------------------------------------------------------------ fresh confirmation (§3.2)
    def test_a_confirmation_lasts_ten_minutes_and_is_bound_to_the_person(env, monkeypatch):
        ana, ben = _user("ana"), _user("ben")
        c = _confirm(_web(ana))
        cookie = c.cookies.get(confirm.COOKIE)
        claims = authn.jwt_decode(cookie)
        assert claims["typ"] == "confirm" and claims["sub"] == ana.id
        assert claims["exp"] - claims["iat"] == 600
        set_cookie = _web(ana).post("/account/confirm", data={
            "password": PW, "next": "/account/export"}).headers["set-cookie"].lower()
        assert "httponly" in set_cookie and "max-age=600" in set_cookie
        assert "path=/account" in set_cookie

        class Req:
            def __init__(self, value):
                self.cookies = {confirm.COOKIE: value}
        assert confirm.is_fresh(Req(cookie), ana)
        assert not confirm.is_fresh(Req(cookie), ben)              # someone else's
        assert not confirm.is_fresh(Req(""), ana)
        assert not confirm.is_fresh(Req(authn.issue_web_token(ana)), ana)   # a session isn't one
        with SessionLocal() as s:                                  # "sign out everywhere" ends it
            row = s.get(m.User, ana.id)
            authn.end_sessions(s, row)
            s.commit()
            assert not confirm.is_fresh(Req(cookie), row)
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + 601)    # …and so do 10 minutes
        assert not confirm.is_fresh(Req(cookie), ana)

    def test_confirming_only_ever_returns_to_an_account_page(env):
        c = _web(_user("ana"))
        for bad in ("https://evil.example.test/", "//evil.example.test", "/trips", "/\\evil",
                    "/account/export/../../trips", "/account/export?x=//evil.example.test",
                    "/account/tokens"):
            r = c.post("/account/confirm", data={"password": PW, "next": bad})
            assert r.headers["location"] == "/account", bad
        assert confirm.safe_next("/account/export") == "/account/export"

    def test_wrong_passwords_are_throttled(env, monkeypatch):
        monkeypatch.setattr(confirm._attempts, "limit", 3)
        c = _web(_user("ana"))
        for _ in range(3):
            assert c.post("/account/confirm", data={"password": "nope"}).status_code == 400
        assert c.post("/account/confirm", data={"password": PW}).status_code == 429

    def test_a_proxy_account_types_its_username(env, monkeypatch):
        monkeypatch.setattr(settings, "auth_proxy_header_trust", True)
        monkeypatch.setattr(settings, "auth_proxy_header", "X-Arx-User")
        pia = _user("pia", source="proxy")
        c = TestClient(app, follow_redirects=False, headers={"X-Arx-User": "arx-pia"})
        page = c.get("/account/confirm?next=/account/export")
        assert page.status_code == 200 and "what protects this step" in page.text
        assert 'name="password"' not in page.text
        assert c.post("/account/confirm", data={"username": "arx-ana"}).status_code == 400
        r = c.post("/account/confirm", data={"username": "arx-pia", "next": "/account/export"})
        assert r.status_code == 303 and confirm.COOKIE in r.headers["set-cookie"]
        assert pia.id == authn.jwt_decode(c.cookies.get(confirm.COOKIE))["sub"]

    @pytest.fixture()
    def sso(monkeypatch):
        """An SSO provider answered from memory; `answer` is its next id_token."""
        from app import oidc
        answer = {}
        for k, v in (("oidc_enabled", True), ("oidc_issuer", "https://idp.example.test"),
                     ("oidc_client_id", "arx-rundere"), ("oidc_hosts", ""),
                     ("oidc_force_hosts", ""), ("oidc_redirect_url", "")):
            monkeypatch.setattr(settings, k, v)
        monkeypatch.setattr(oidc, "discovery", lambda: {
            "issuer": "https://idp.example.test",
            "authorization_endpoint": "https://idp.example.test/authorize",
            "token_endpoint": "https://idp.example.test/token"})
        monkeypatch.setattr(oidc, "exchange_code", lambda *a, **k: {"id_token": "x.y.z"})
        monkeypatch.setattr(oidc, "decode_id_token", lambda tok: dict(answer))
        return answer

    def _sso_round_trip(c, answer, **claims):
        r = c.get("/account/confirm/sso?next=/account/export")
        assert r.status_code == 303
        q = parse_qs(urlsplit(r.headers["location"]).query)
        assert r.headers["location"].startswith("https://idp.example.test/authorize?")
        assert (q["prompt"], q["max_age"]) == (["login"], ["0"])     # sign in again
        answer.clear()
        answer.update({"iss": "https://idp.example.test", "aud": "arx-rundere",
                       "exp": time.time() + 300, "nonce": q["nonce"][0], **claims})
        return c.get(f"/oidc/callback?code=arx-code&state={q['state'][0]}")

    def test_an_sso_account_confirms_by_signing_in_again(env, sso):
        sam = _user("sam", source="oidc", external_subject="oidc:arx-sam-sub")
        c = _web(sam)
        page = c.get("/account/confirm?next=/account/export")
        assert "sign in again" in page.text and 'name="password"' not in page.text
        # Typing something is never enough for an SSO account.
        assert c.post("/account/confirm", data={"password": PW,
                                                "username": "arx-sam"}).status_code == 400
        assert confirm.COOKIE not in c.cookies
        r = _sso_round_trip(c, sso, sub="arx-sam-sub", auth_time=time.time() - 5)
        assert r.status_code == 303 and r.headers["location"] == "/account/export"
        assert authn.jwt_decode(c.cookies.get(confirm.COOKIE))["sub"] == sam.id
        # It confirmed; it didn't sign anyone in or make an account.
        assert settings.auth_cookie_name not in r.headers.get("set-cookie", "")
        with SessionLocal() as s:
            assert s.query(m.User).filter(m.User.username.like("arx-%")).count() == 1

    def test_an_sso_provider_that_omits_auth_time_still_confirms(env, sso):
        c = _web(_user("sam", source="oidc", external_subject="oidc:arx-sam-sub"))
        r = _sso_round_trip(c, sso, sub="arx-sam-sub")
        assert r.headers["location"] == "/account/export" and confirm.COOKIE in c.cookies

    def test_an_sso_round_trip_that_didnt_sign_in_again_is_refused(env, sso):
        sam = _user("sam", source="oidc", external_subject="oidc:arx-sam-sub")
        c = _web(sam)
        r = _sso_round_trip(c, sso, sub="arx-sam-sub", auth_time=time.time() - 3600)
        assert r.headers["location"].startswith("/account/confirm?") and "error=" in r.headers["location"]
        assert confirm.COOKIE not in c.cookies
        # A different person at the provider confirms nothing either.
        r = _sso_round_trip(c, sso, sub="arx-somebody-else", auth_time=time.time())
        assert "error=" in r.headers["location"] and confirm.COOKIE not in c.cookies
        with SessionLocal() as s:                  # …and isn't provisioned as a new account
            assert s.query(m.User).filter_by(external_subject="oidc:arx-somebody-else").count() == 0
        # The handshake belongs to the browser that started it.
        r1 = c.get("/account/confirm/sso?next=/account/export")
        q = parse_qs(urlsplit(r1.headers["location"]).query)
        sso.update({"sub": "arx-sam-sub", "nonce": q["nonce"][0], "auth_time": time.time()})
        other = _web(_user("ben"))
        other.cookies.set("yonder_oidc", c.cookies.get("yonder_oidc"))
        r = other.get(f"/oidc/callback?code=arx-code&state={q['state'][0]}")
        assert "error=" in r.headers["location"] and confirm.COOKIE not in other.cookies
