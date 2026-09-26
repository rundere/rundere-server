"""YONDER-108 — the remote MCP server at /mcp (Streamable HTTP, stateless JSON).

Auth (401 + resource-metadata pointer; bearer only), the tool list, a happy
path per tool, per-user scoping and admin sharing through every tool family,
the research-finding tool landing on the caller's Discover only, and the
assign-trip filing rule (API + tool). SQLite via conftest.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import authn, oauth
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (AccountEvent, Base, DiscoveryFinding, IngestBatch,
                            JournalEntry, OAuthGrant, PackingItem, PackingPlan,
                            PersonalApiToken, TravelerIdentity, TravelItem, Trip,
                            TripActivity, TripTraveler, User)

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False

    ALL_TOOLS = {"list_trips", "get_trip", "update_trip", "dashboard",
                 "list_activity_types", "add_activity", "update_activity",
                 "delete_activity", "generate_packing", "set_packing_status",
                 "add_packing_item", "create_journal_entry", "list_journal_entries",
                 "get_journal_entry", "update_journal_entry", "attach_journal_entry",
                 "capture_url", "list_review", "get_review_item",
                 "assign_review_item_to_trip", "save_research"}

    @pytest.fixture(scope="module")
    def client():
        with TestClient(app) as c:          # runs the lifespan (MCP session manager)
            yield c

    def _teardown(s):
        tids = [t.id for t in s.query(Trip).filter(Trip.name.like("MX %")).all()]
        for model in (TripTraveler, TripActivity, DiscoveryFinding, PackingItem, PackingPlan):
            s.query(model).filter(model.trip_id.in_(tids)).delete(synchronize_session=False)
        s.query(Trip).filter(Trip.id.in_(tids)).delete(synchronize_session=False)
        uids = [u.id for u in s.query(User).filter(User.username.like("mx-%")).all()]
        s.query(TravelItem).filter(TravelItem.owner_id.in_(uids)).delete(synchronize_session=False)
        s.query(IngestBatch).filter(IngestBatch.user_id.in_(uids)).delete(synchronize_session=False)
        s.query(JournalEntry).filter(JournalEntry.owner_id.in_(uids)).delete(synchronize_session=False)
        s.query(PersonalApiToken).filter(PersonalApiToken.user_id.in_(uids)).delete(
            synchronize_session=False)
        s.query(OAuthGrant).filter(OAuthGrant.user_id.in_(uids)).delete(synchronize_session=False)
        s.query(AccountEvent).filter(AccountEvent.user_id.in_(uids)).delete(synchronize_session=False)
        s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
        s.query(TravelerIdentity).filter(TravelerIdentity.display_name.like("MX %")).delete(
            synchronize_session=False)
        s.commit()

    @pytest.fixture()
    def w(monkeypatch):
        """alice + bob travel together on "MX Shared"; bob also has a private
        trip and has opted out of sharing; carol shares; the admin is nobody's
        traveller."""
        monkeypatch.setattr(settings, "auth_proxy_header_trust", False)
        with SessionLocal() as s:
            _teardown(s)
            ta, tb = TravelerIdentity(display_name="MX Alice", kind="guest"), \
                TravelerIdentity(display_name="MX Bob", kind="guest")
            s.add_all([ta, tb]); s.flush()
            alice = User(username="mx-alice", auth_source="local", traveler_id=ta.id,
                         password_hash=authn.hash_password("pw"))
            bob = User(username="mx-bob", auth_source="local", traveler_id=tb.id,
                       share_with_admins=False, password_hash=authn.hash_password("pw"))
            carol = User(username="mx-carol", auth_source="local")
            admin = User(username="mx-admin", auth_source="local", is_admin=True)
            s.add_all([alice, bob, carol, admin]); s.flush()
            shared = Trip(name="MX Shared", source="manual", owner_id=alice.id,
                          start_date="2027-06-01", end_date="2027-06-08")
            private = Trip(name="MX Bob Private", source="manual", owner_id=bob.id)
            carols = Trip(name="MX Carol", source="manual", owner_id=carol.id)
            s.add_all([shared, private, carols]); s.flush()
            s.add_all([TripTraveler(trip_id=shared.id, traveler_id=ta.id, role="owner"),
                       TripTraveler(trip_id=shared.id, traveler_id=tb.id, role="companion")])
            act = TripActivity(trip_id=shared.id, type="note", starts_at="2027-06-02T09:00",
                               values={"title": "Pack snacks", "when": "2027-06-02T09:00"})
            bob_note = JournalEntry(owner_id=bob.id, entry_date="2027-06-01", body="mx bob diary",
                                    source="web")
            admin_cap = TravelItem(owner_id=admin.id, canonical_key="mx:admincap", source={},
                                   status={})
            alice_cap = TravelItem(owner_id=alice.id, canonical_key="mx:alicecap", source={},
                                   status={})
            s.add_all([act, bob_note, admin_cap, alice_cap]); s.commit()
            ids = {"alice": alice.id, "bob": bob.id, "carol": carol.id, "admin": admin.id,
                   "ta": ta.id, "tb": tb.id, "shared": shared.id, "private": private.id,
                   "carols": carols.id, "act": act.id, "bob_note": bob_note.id,
                   "admin_cap": admin_cap.id, "alice_cap": alice_cap.id}
        yield ids
        with SessionLocal() as s:
            _teardown(s)

    def _tok(uid):
        with SessionLocal() as s:
            return oauth.create_pat(s, s.get(User, uid), "mcp test")[1]

    _H = {"accept": "application/json, text/event-stream",
          "mcp-protocol-version": "2025-06-18"}

    def _rpc(client, tok, method, params=None, headers=None):
        h = {**_H, **({"authorization": f"Bearer {tok}"} if tok else {}), **(headers or {})}
        return client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1,
                                                    "method": method, "params": params or {}})

    def call(client, tok, tool, /, **args):
        """→ (ok, payload). payload = the tool's JSON result, or its error text."""
        r = _rpc(client, tok, "tools/call", {"name": tool, "arguments": args})
        assert r.status_code == 200, r.text
        res = r.json()["result"]
        if res.get("isError"):
            return False, res["content"][0]["text"]
        if "structuredContent" in res:
            return True, res["structuredContent"]
        return True, json.loads(res["content"][0]["text"])

    def ok(client, tok, tool, /, **args):
        good, out = call(client, tok, tool, **args)
        assert good, f"{tool} failed: {out}"
        return out

    # ------------------------------------------------------------------ auth
    def test_401_without_token_points_at_metadata(client):
        r = client.post("/mcp", headers=_H, json={"jsonrpc": "2.0", "id": 1,
                                                  "method": "tools/list"})
        assert r.status_code == 401
        www = r.headers["www-authenticate"]
        assert www.startswith("Bearer ")
        assert 'resource_metadata="http://testserver/.well-known/oauth-protected-resource"' in www
        assert "invalid_token" not in www
        meta = client.get("/.well-known/oauth-protected-resource").json()
        assert meta["resource"] == "http://testserver/mcp"

    def test_401_with_bad_token(client):
        r = _rpc(client, "ypat_nope", "tools/list")
        assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]
        r = _rpc(client, "not.a.jwt", "tools/list")
        assert r.status_code == 401

    def test_401_even_with_auth_gate_on(client, monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        r = client.post("/mcp", headers=_H, json={}, follow_redirects=False)
        assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]

    def test_cookie_and_proxy_header_not_accepted(client, w, monkeypatch):
        with SessionLocal() as s:
            web = authn.issue_web_token(s.get(User, w["alice"]))
        r = client.post("/mcp", headers=_H, cookies={settings.auth_cookie_name: web},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
        monkeypatch.setattr(settings, "auth_proxy_header_trust", True)
        r = client.post("/mcp", headers={**_H, "x-authentik-username": "mx-alice"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401

    def test_revoked_token_loses_access(client, w):
        tok = _tok(w["alice"])
        assert _rpc(client, tok, "tools/list").status_code == 200
        with SessionLocal() as s:
            row = s.query(PersonalApiToken).filter_by(user_id=w["alice"]).one()
            oauth.revoke_pat(s, s.get(User, w["alice"]), row.id)
        assert _rpc(client, tok, "tools/list").status_code == 401

    def test_oauth_access_token_works(client, w):
        """The token a claude.ai connector gets (YONDER-107) works on /mcp."""
        with SessionLocal() as s:
            cl, _ = oauth.register_client(s, name="Claude", redirect_uris=[
                "https://claude.ai/api/mcp/auth_callback"])
            u = s.get(User, w["alice"])
            g = OAuthGrant(client_id=cl.id, user_id=u.id, token_version=u.token_version,
                           expires_at=oauth._now() + oauth.timedelta(days=1))
            s.add(g); s.commit()
            at = oauth.issue_access(u, g, "http://testserver/mcp")
        assert {t["name"] for t in ok(client, at, "list_trips")["trips"]} >= {"MX Shared"}
        with SessionLocal() as s:
            oauth.revoke_grant(s, s.get(OAuthGrant, g.id))
        assert _rpc(client, at, "tools/list").status_code == 401

    # ------------------------------------------------------------------ protocol
    def test_initialize_and_tool_list(client, w):
        tok = _tok(w["alice"])
        r = _rpc(client, tok, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "test", "version": "1"}})
        assert r.status_code == 200
        info = r.json()["result"]
        assert info["serverInfo"]["name"] == "Yonder" and "save_research" in info["instructions"]
        tools = _rpc(client, tok, "tools/list").json()["result"]["tools"]
        assert {t["name"] for t in tools} == ALL_TOOLS
        for t in tools:
            assert t["description"], t["name"]
        add = next(t for t in tools if t["name"] == "add_activity")
        assert "traveler_ids" in add["inputSchema"]["properties"]
        assert "description" in add["inputSchema"]["properties"]["traveler_ids"]

    # ------------------------------------------------------------------ tools: happy paths
    def test_trip_tools(client, w):
        tok = _tok(w["alice"])
        trips = {t["name"]: t for t in ok(client, tok, "list_trips")["trips"]}
        assert "MX Shared" in trips and "MX Bob Private" not in trips
        snap = ok(client, tok, "get_trip", trip_id=w["shared"])
        assert snap["trip"]["name"] == "MX Shared"
        assert {t["id"] for t in snap["travelers"]} == {w["ta"], w["tb"]}
        assert snap["activities"][0]["traveler_ids"] == []
        out = ok(client, tok, "update_trip", trip_id=w["shared"], name="MX Shared (renamed)",
                 notes="bring boots")
        assert out["name"] == "MX Shared (renamed)"
        with SessionLocal() as s:
            t = s.get(Trip, w["shared"])
            assert t.notes == "bring boots" and t.start_date == "2027-06-01"
        d = ok(client, tok, "dashboard")
        assert "counts" in d and "trip_count" in d

    def test_timeline_tools(client, w):
        tok = _tok(w["alice"])
        types = {t["key"]: t for t in ok(client, tok, "list_activity_types")["types"]}
        assert "dining" in types and any(f["key"] == "when" for f in types["dining"]["fields"])
        a = ok(client, tok, "add_activity", trip_id=w["shared"], type="dining",
               values={"name": "MX Noma", "when": "2027-06-03T19:00", "partySize": "2"},
               traveler_ids=[w["tb"]])
        assert a["starts_at"] == "2027-06-03T19:00" and a["traveler_ids"] == [w["tb"]]
        assert a["title"]
        u = ok(client, tok, "update_activity", trip_id=w["shared"], activity_id=a["id"],
               values={"partySize": "4", "address": None}, traveler_ids=[])
        assert u["values"]["name"] == "MX Noma" and u["values"]["partySize"] == "4"
        assert u["traveler_ids"] == []
        good, err = call(client, tok, "add_activity", trip_id=w["shared"], type="dining",
                         values={"name": "x"}, traveler_ids=[999999])
        assert not good and "not travellers on this trip" in err
        good, err = call(client, tok, "add_activity", trip_id=w["shared"], type="spaceship",
                         values={})
        assert not good and "unknown activity type" in err
        assert ok(client, tok, "delete_activity", trip_id=w["shared"], activity_id=a["id"])["ok"]
        with SessionLocal() as s:
            assert s.get(TripActivity, a["id"]) is None

    def test_packing_tools(client, w):
        tok = _tok(w["alice"])
        plan = ok(client, tok, "generate_packing", trip_id=w["shared"])
        assert "items" in plan
        item = ok(client, tok, "add_packing_item", trip_id=w["shared"], label="MX bear spray",
                  category="activity", quantity=2)
        assert item["quantity"] == 2 and item["status"] == "draft"
        done = ok(client, tok, "set_packing_status", trip_id=w["shared"], item_id=item["id"],
                  status="packed")
        assert done["status"] == "packed"
        good, _ = call(client, tok, "set_packing_status", trip_id=w["shared"],
                       item_id=item["id"], status="lost")
        assert not good                       # schema rejects it
        # bob can't touch alice's packing line on the same trip
        good, err = call(client, _tok(w["bob"]), "set_packing_status", trip_id=w["shared"],
                         item_id=item["id"], status="removed")
        assert not good and "not found" in err

    def test_journal_tools(client, w):
        tok = _tok(w["alice"])
        e = ok(client, tok, "create_journal_entry", body="MX first day", entry_date="2027-06-01",
               place_label="Banff")
        assert e["source"] == "mcp" and e["trip_id"] is None
        assert ok(client, tok, "get_journal_entry", entry_id=e["id"])["body"] == "MX first day"
        e2 = ok(client, tok, "update_journal_entry", entry_id=e["id"], body="MX first day!")
        assert e2["body"] == "MX first day!" and e2["place_label"] == "Banff"
        e3 = ok(client, tok, "attach_journal_entry", entry_id=e["id"], trip_id=w["shared"])
        assert e3["trip_id"] == w["shared"]
        listed = ok(client, tok, "list_journal_entries", trip_id=w["shared"])["entries"]
        assert [x["id"] for x in listed] == [e["id"]]
        assert ok(client, tok, "attach_journal_entry", entry_id=e["id"], trip_id=None)["trip_id"] is None
        assert ok(client, tok, "list_journal_entries", trip_id=w["shared"])["entries"] == []

    def test_capture_and_review_tools(client, w):
        tok = _tok(w["alice"])
        good, err = call(client, tok, "capture_url",
                         url="https://www.google.com/maps/dir/Banff/Lake+Louise")
        assert not good and "travel_date" in err
        out = ok(client, tok, "capture_url",
                 url="https://www.google.com/maps/dir/Banff/Lake+Louise",
                 travel_date="2027-06-04", mode="car")
        with SessionLocal() as s:
            assert s.get(IngestBatch, out["batch_id"]).user_id == w["alice"]
        rv = ok(client, tok, "list_review")
        assert "counts" in rv and "items" in rv
        assert w["alice_cap"] in {i["id"] for i in rv["items"]}
        assert ok(client, tok, "get_review_item", item_id=w["alice_cap"])["id"] == w["alice_cap"]
        res = ok(client, tok, "assign_review_item_to_trip", item_id=w["alice_cap"],
                 trip_id=w["shared"])
        assert res["trip_id"] == w["shared"]
        with SessionLocal() as s:
            item = s.get(TravelItem, w["alice_cap"])
            assert item.trip_id == w["shared"] and item.review["trip_pinned"] is True

    # ------------------------------------------------------------------ Discover research
    def test_save_research_lands_on_callers_discover_only(client, w):
        tok = _tok(w["alice"])
        f = ok(client, tok, "save_research", trip_id=w["shared"], title="Day hikes",
               body_md="- Johnston Canyon\n- Sulphur Skyline",
               sources=[{"title": "Parks Canada", "url": "https://parks.canada.ca/banff"},
                        {"title": "bad", "url": "javascript:alert(1)"}])
        assert f["topic"] == "research" and f["status"] == "draft" and f["place_key"] == "trip"
        assert f["body_md"].startswith("**Day hikes**")
        assert f["sources"] == [{"title": "Parks Canada", "url": "https://parks.canada.ca/banff"}]
        # upsert on (trip, user, topic, place)
        f2 = ok(client, tok, "save_research", trip_id=w["shared"], title="Day hikes v2",
                body_md="- Plain of Six Glaciers")
        assert f2["id"] == f["id"] and "v2" in f2["body_md"]
        assert f2["data"]["history"][0]["title"] == "Day hikes"
        with SessionLocal() as s:              # the person approves it on Discover…
            s.get(DiscoveryFinding, f["id"]).status = "approved"
            s.commit()
        # …and a later refresh (a concert got announced) still supersedes it —
        # research is never locked in place; the approved version becomes history.
        f3 = ok(client, tok, "save_research", trip_id=w["shared"], title="Day hikes v3",
                body_md="- new concert announced")
        assert f3["id"] == f["id"] and f3["status"] == "draft" and "v3" in f3["body_md"]
        hist = f3["data"]["history"]
        assert [h["status"] for h in hist] == ["approved", "draft"]
        assert "v2" in hist[0]["body_md"] and "Johnston" in hist[1]["body_md"]
        with SessionLocal() as s:
            from app.trips.routes import _finding_view
            view = _finding_view(s.get(DiscoveryFinding, f["id"]))
            assert len(view["history"]) == 2 and view["history"][0]["status"] == "approved"
        from discover.policy import HISTORY_KEPT as RESEARCH_HISTORY_KEPT
        for i in range(RESEARCH_HISTORY_KEPT + 2):
            last = ok(client, tok, "save_research", trip_id=w["shared"], title=f"r{i}", body_md="x")
        assert len(last["data"]["history"]) == RESEARCH_HISTORY_KEPT
        city = ok(client, tok, "save_research", trip_id=w["shared"], title="Banff food",
                  body_md="x", place_key="CA:Banff")
        assert city["id"] != f["id"]
        with SessionLocal() as s:
            rows = s.query(DiscoveryFinding).filter_by(trip_id=w["shared"], topic="research").all()
            assert {r.user_id for r in rows} == {w["alice"]}
            assert {r.resolver for r in rows} == {"claude"}
            from app.trips.routes import _discovery_context
            groups = _discovery_context(s, w["shared"], w["alice"])["discovery_topics"]
            research = next(g for g in groups if g["topic"] == "research")
            assert research["label"] == "Research (Claude)"
            assert all(not v["rerunnable"] for v in research["findings"])
            assert not any(g["topic"] == "research" for g in
                           _discovery_context(s, w["shared"], w["bob"])["discovery_topics"])
        mine = ok(client, tok, "get_trip", trip_id=w["shared"])["discovery"]
        assert {d["id"] for d in mine} >= {f["id"], city["id"]}
        # bob travels on the same trip but sees none of it
        theirs = ok(client, _tok(w["bob"]), "get_trip", trip_id=w["shared"])["discovery"]
        assert not any(d["topic"] == "research" for d in theirs)

    def test_research_survives_a_discover_run(client, w):
        tok = _tok(w["alice"])
        f = ok(client, tok, "save_research", trip_id=w["shared"], title="Keep me", body_md="x")
        from discover.runner import _prune_stale_drafts
        with SessionLocal() as s:
            _prune_stale_drafts(s, w["shared"], set(), w["alice"])
            s.commit()
            assert s.get(DiscoveryFinding, f["id"]) is not None

    def test_research_api_endpoint(client, w):
        tok = _tok(w["alice"])
        r = client.post(f"/api/v1/trips/{w['shared']}/findings",
                        headers={"Authorization": f"Bearer {tok}"},
                        json={"title": "Via API", "body_md": "hello"})
        assert r.status_code == 201 and r.json()["topic"] == "research"
        r = client.post(f"/api/v1/trips/{w['shared']}/findings",
                        headers={"Authorization": f"Bearer {tok}"},
                        json={"title": " ", "body_md": "hello"})
        assert r.status_code == 400

    # ------------------------------------------------------------------ scoping
    def test_private_trip_unreachable_through_every_tool(client, w):
        tok = _tok(w["alice"])
        p = w["private"]
        attempts = [
            ("get_trip", {"trip_id": p}),
            ("update_trip", {"trip_id": p, "name": "hacked"}),
            ("add_activity", {"trip_id": p, "type": "note", "values": {"title": "x"}}),
            ("delete_activity", {"trip_id": p, "activity_id": w["act"]}),
            ("generate_packing", {"trip_id": p}),
            ("add_packing_item", {"trip_id": p, "label": "x"}),
            ("save_research", {"trip_id": p, "title": "x", "body_md": "y"}),
            ("create_journal_entry", {"body": "x", "trip_id": p}),
            ("assign_review_item_to_trip", {"item_id": w["alice_cap"], "trip_id": p}),
            ("get_journal_entry", {"entry_id": w["bob_note"]}),
            ("update_journal_entry", {"entry_id": w["bob_note"], "body": "hacked"}),
            ("get_review_item", {"item_id": w["admin_cap"]}),
        ]
        for name, args in attempts:
            good, err = call(client, tok, name, **args)
            assert not good and "404" in err, (name, err)
        with SessionLocal() as s:
            assert s.get(Trip, p).name == "MX Bob Private"
            assert s.get(JournalEntry, w["bob_note"]).body == "mx bob diary"
            assert s.query(DiscoveryFinding).filter_by(trip_id=p).count() == 0
        assert "mx bob diary" not in json.dumps(ok(client, tok, "list_journal_entries"))

    def test_admin_does_not_see_opted_out_data(client, w):
        tok = _tok(w["admin"])
        names = {t["name"] for t in ok(client, tok, "list_trips")["trips"]}
        assert "MX Carol" in names                    # carol shares with admins
        assert "MX Bob Private" not in names          # bob opted out
        good, err = call(client, tok, "get_trip", trip_id=w["private"])
        assert not good and "404" in err
        good, err = call(client, tok, "get_journal_entry", entry_id=w["bob_note"])
        assert not good

    def test_assign_uses_filing_rule_tool_and_api(client, w):
        """An admin can SEE carol's trip (she shares) but must not file their own
        capture into it — the web picker's filing rule, now on the API + MCP."""
        tok = _tok(w["admin"])
        good, err = call(client, tok, "assign_review_item_to_trip", item_id=w["admin_cap"],
                         trip_id=w["carols"])
        assert not good and "403" in err
        r = client.post(f"/api/v1/review/{w['admin_cap']}/assign-trip",
                        headers={"Authorization": f"Bearer {tok}"},
                        json={"trip_id": w["carols"]})
        assert r.status_code == 403
        with SessionLocal() as s:
            assert s.get(TravelItem, w["admin_cap"]).trip_id is None
        # a trip the user travels on (not owner) is fine
        bob = _tok(w["bob"])
        with SessionLocal() as s:
            s.add(TravelItem(owner_id=w["bob"], canonical_key="mx:bobcap", source={}, status={}))
            s.commit()
            bob_cap = s.query(TravelItem).filter_by(canonical_key="mx:bobcap").one().id
        assert ok(client, bob, "assign_review_item_to_trip", item_id=bob_cap,
                  trip_id=w["shared"])["trip_id"] == w["shared"]

    # ------------------------------------------------------------------ Discover: lock + external writes
    def _local_finding(trip_id, uid, topic, place_key, status="draft", body="local body"):
        with SessionLocal() as s:
            f = DiscoveryFinding(trip_id=trip_id, user_id=uid, topic=topic, scope="country",
                                 place_key=place_key, status=status, body_md=body,
                                 resolver="llm")
            s.add(f)
            s.commit()
            return f.id

    def test_claude_updates_standard_topic_and_local_run_leaves_it(client, w):
        tok = _tok(w["alice"])
        fid = _local_finding(w["shared"], w["alice"], "food", "CA", status="approved",
                             body="local food (approved)")
        f = ok(client, tok, "save_research", trip_id=w["shared"], topic="food",
               place_key="CA", title="Where to eat", body_md="- Farm & Fire")
        assert f["id"] == fid and f["external"] and not f["locked"] and f["status"] == "draft"
        assert f["data"]["history"][0]["body_md"] == "local food (approved)"
        with SessionLocal() as s:
            from discover import policy
            assert not policy.local_run_may_replace(s.get(DiscoveryFinding, fid))
        good, err = call(client, tok, "save_research", trip_id=w["shared"], topic="food",
                         title="x", body_md="y")
        assert not good and "place_key is required" in err
        good, _ = call(client, tok, "save_research", trip_id=w["shared"], topic="weather",
                       place_key="CA", title="x", body_md="y")
        assert not good                               # schema: weather isn't Claude-writable
        r = client.post(f"/api/v1/trips/{w['shared']}/findings",
                        headers={"Authorization": f"Bearer {tok}"},
                        json={"topic": "currency", "place_key": "CA", "title": "x", "body_md": "y"})
        assert r.status_code == 400

    def test_lock_is_absolute_for_claude(client, w):
        tok = _tok(w["alice"])
        fid = _local_finding(w["shared"], w["alice"], "visa", "CA", status="approved",
                             body="eTA required — checked")
        snap = ok(client, tok, "get_trip", trip_id=w["shared"])
        mine = next(d for d in snap["discovery"] if d["id"] == fid)
        assert mine["locked"] is True and mine["external"] is False
        good, err = call(client, tok, "save_research", trip_id=w["shared"], topic="visa",
                         place_key="CA", title="Visa", body_md="changed")
        assert not good and "409" in err and "locked" in err
        with SessionLocal() as s:
            assert s.get(DiscoveryFinding, fid).body_md == "eTA required — checked"

    def test_web_lock_unlock_and_edit_keeps_history(client, w):
        fid = _local_finding(w["shared"], None, "visa", "CA")
        wid = _local_finding(w["shared"], None, "weather", "CA")
        with SessionLocal() as s:           # a finding with earlier versions
            f = s.get(DiscoveryFinding, fid)
            f.data = {"history": [{"body_md": "older", "status": "draft"}]}
            s.commit()
        r = client.post(f"/trips/{w['shared']}/finding/{fid}/lock", data={"lock": "1"})
        assert r.status_code == 200 and r.headers.get("hx-redirect")
        with SessionLocal() as s:
            assert s.get(DiscoveryFinding, fid).status == "approved"
        page = client.get(f"/trips/{w['shared']}").text
        assert "🔒 locked" in page and "Unlock" in page
        client.post(f"/trips/{w['shared']}/finding/{fid}/lock", data={"lock": "0"})
        with SessionLocal() as s:
            assert s.get(DiscoveryFinding, fid).status == "draft"
        assert client.post(f"/trips/{w['shared']}/finding/{wid}/lock",
                           data={"lock": "1"}).status_code == 400   # weather never locks
        client.post(f"/trips/{w['shared']}/finding/{fid}/edit", data={"body": "my edit"})
        with SessionLocal() as s:
            f = s.get(DiscoveryFinding, fid)
            assert f.status == "edited" and f.data["history"][0]["body_md"] == "older"
