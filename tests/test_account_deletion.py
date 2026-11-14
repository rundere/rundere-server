"""SPEC-13 §3 and §4 (D-242) — deleting an account.

The tests §4.4 asks for: the survivor test (deleting one account changes
nobody else's rows beyond the listed exceptions), the id scan (the leaver's id
is in no column and no folder name afterwards), scoping, the guards, and the
lock on a pending account's tokens, keys and mail. Plus the request, the
cancel, what the purge decides again after the 7 days, and the daily step.

All data is invented. The survivor test and the id scan also run against
Postgres when TX_DATABASE_URL points at one (§4.4 point 8)."""
import json
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
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
    import sqlalchemy as sa

    from app import account_deletion as deletion
    from app import (account_purge, archive_jobs, auth, authn, cli, confirm, documents,
                     inbox, oauth, ownership, retention)
    from app import models as m
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app

    m.Base.metadata.create_all(engine)
    PW = "del-correct-horse"
    _made: list[tuple] = []            # (model, primary key) of every fixture row

    # ------------------------------------------------------------------ fixtures
    @pytest.fixture(autouse=True)
    def env(tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        monkeypatch.setattr(settings, "auth_cookie_secure", False)
        monkeypatch.setattr(settings, "export_dir", str(tmp_path / "exports"))
        monkeypatch.setattr(settings, "documents_dir", str(tmp_path / "docs"))
        monkeypatch.setattr(settings, "public_base_url", "")
        monkeypatch.setattr(settings, "smtp_host", "")
        confirm._attempts._hits.clear()
        # Other test files leave admins behind in the shared database; the
        # last-admin guard must see only this file's accounts.
        with SessionLocal() as s:
            before = [u.id for u in s.query(m.User).filter(m.User.is_admin.is_(True))]
            s.query(m.User).filter(m.User.id.in_(before)).update(
                {"is_admin": False}, synchronize_session=False)
            s.commit()
        yield tmp_path
        _cleanup()
        with SessionLocal() as s:
            s.query(m.User).filter(m.User.id.in_(before)).update(
                {"is_admin": True}, synchronize_session=False)
            s.commit()

    def _cleanup():
        with SessionLocal() as s:
            # Rows the app made for this file's accounts, which Postgres holds
            # to their user by a foreign key.
            uids = [pk[0] for model, pk in _made if model is m.User]
            for model in (m.RefreshToken, m.PasswordResetToken):
                s.query(model).filter(model.user_id.in_(uids)).delete(synchronize_session=False)
            s.commit()
            for model, pk in reversed(_made):
                row = s.get(model, pk)
                if row is not None:
                    s.delete(row)
                    s.flush()
            s.query(m.AccountDeletion).delete(synchronize_session=False)
            s.query(m.AccountEvent).filter(m.AccountEvent.action.in_(
                ("account_deleted", "deletion_requested", "deletion_cancelled"))).delete(
                synchronize_session=False)
            s.query(m.InboxMessage).filter(
                m.InboxMessage.outcome == inbox.ACCOUNT_LOCKED).delete(synchronize_session=False)
            s.commit()
        _made.clear()

    def _value(col):
        """A value for a column the test didn't set and the model doesn't default."""
        t = col.type
        if isinstance(t, (sa.JSON,)):
            return {}
        if isinstance(t, sa.Boolean):
            return False
        if isinstance(t, sa.DateTime):
            return datetime.now(timezone.utc)
        if isinstance(t, sa.Date):
            return date(2031, 5, 4)
        if isinstance(t, sa.Numeric):
            return Decimal("12.50")
        if isinstance(t, sa.Float):
            return 1.5
        if isinstance(t, sa.Integer):
            return 1
        text = uuid.uuid4().hex
        return text[: t.length] if getattr(t, "length", None) else text

    def _mk(s, model, **kw):
        """Insert one row, filling every required column the caller left out."""
        for col in model.__table__.columns:
            if col.name in kw or col.nullable or col.default is not None \
                    or col.server_default is not None:
                continue
            if col.primary_key and col.autoincrement is True and isinstance(col.type, sa.Integer):
                continue
            kw[col.name] = _value(col)
        row = model(**kw)
        s.add(row)
        s.flush()
        _made.append((model, sa.inspect(row).identity))
        return row

    def _user(s, name, *, admin=False, source="local", email=True, traveller=True, **kw):
        t = _mk(s, m.TravelerIdentity, display_name=f"Del {name.title()} {uuid.uuid4().hex[:6]}",
                kind="self", aliases=[f"{name} alias"], nationality="NZ",
                base_currency="NZD", timezone="Pacific/Auckland", notes="a note",
                airtrail_user_id=f"at-{name}", generic=False) if traveller else None
        return _mk(s, m.User, username=f"del-{name}-{uuid.uuid4().hex[:6]}", auth_source=source,
                   is_admin=admin, is_active=True, token_version=0,
                   email=f"{name}-{uuid.uuid4().hex[:6]}@example.test" if email else None,
                   password_hash=authn.hash_password(PW) if source == "local" else None,
                   traveler_id=t.id if t else None, **kw)

    def _own_rows(s, u, tmp):
        """One row of `u`'s in every table that holds a person's own data."""
        uid, tid = u.id, u.traveler_id
        for model in (m.RefreshToken, m.PasswordResetToken, m.OAuthCode, m.OAuthGrant,
                      m.PersonalApiToken, m.InboxAddress, m.ArchiveExport, m.InventoryItem,
                      m.Bag, m.InboxMessage, m.AiCall):
            _mk(s, model, user_id=uid)
        _mk(s, m.UserIntegration, user_id=uid, kind="airtrail", credential_ct="ct-" + uid[:8])
        _mk(s, m.AccountEvent, user_id=uid, actor_id=uid, action="password_set",
            detail=f"about {uid}")
        for model in (m.HomeProfile, m.PackingProfile, m.DiscoveryProfile):
            _mk(s, model, traveler_id=tid)
        _mk(s, m.JournalEntry, owner_id=uid, body="a day out")
        place = _mk(s, m.SavedPlace, owner_id=uid, name="Harbour Kiosk")
        lst = _mk(s, m.PlaceList, owner_id=uid, name="Coffee")
        _mk(s, m.PlaceListMember, owner_id=uid, list_id=lst.id, place_id=place.id)
        tpl = _mk(s, m.PackingTemplate, user_id=uid)
        _mk(s, m.PackingTemplateItem, template_id=tpl.id)
        doc = _mk(s, m.Document, owner_id=uid, filename="ticket.pdf")
        p = documents.path_for(doc)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF fictional")
        folder = archive_jobs.folder_for(uid)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "old.zip").write_bytes(b"zip")
        # A capture: its key, a batch with a forwarded email, an item with its
        # keys and commit attempts, and the artifact both point at.
        key = _mk(s, m.ApiKey, user_id=uid)
        art = _mk(s, m.Artifact)
        batch = _mk(s, m.IngestBatch, api_key_id=key.id, user_id=uid, artifact_sha256=art.sha256,
                    payload={"capture_channel": "email", "subject": "Your booking",
                             "message_id": f"<{uuid.uuid4().hex}@mail.example.test>"})
        item = _mk(s, m.TravelItem, owner_id=uid, batch_id=batch.id, artifact_sha256=art.sha256,
                   revision={"prior_item_id": None})
        _mk(s, m.ItemKey, item_id=item.id)
        _mk(s, m.CommitAttempt, item_id=item.id)
        # Server-wide rows that record who touched them.
        _mk(s, m.ServerSetting, key="del." + uuid.uuid4().hex[:10], updated_by=uid)
        _mk(s, m.NotificationTarget, created_by=uid)
        _mk(s, m.OAuthClient, created_by=uid, redirect_uris=[])
        _mk(s, m.AlertState, key=f"integration:{uid}:airtrail")
        return {"place": place, "key": key, "batch": batch, "item": item, "art": art,
                "doc": doc}

    def _trip(s, name, owner, *, travellers=(), viewers=(), guests=()):
        """A trip with a full set of rows from every account on it."""
        trip = _mk(s, m.Trip, name=f"Del {name}", owner_id=owner.id)
        people = [owner, *travellers]
        for u in people:
            _mk(s, m.TripTraveler, trip_id=trip.id, traveler_id=u.traveler_id, role="traveler")
        for g in guests:
            _mk(s, m.TripTraveler, trip_id=trip.id, traveler_id=g.id, role="traveler")
        for u in viewers:
            _mk(s, m.TripMember, trip_id=trip.id, user_id=u.id, role="viewer")
        act = _mk(s, m.TripActivity, trip_id=trip.id, type="flight",
                  values={"passengers": ["A. Traveller"]})
        for u in people:
            _mk(s, m.TripActivityTraveler, activity_id=act.id, traveler_id=u.traveler_id)
        for u in [*people, *viewers]:
            uid = u.id
            for model in (m.DiscoveryRun, m.DiscoveryFinding, m.DiscoveryExclusion,
                          m.PackingPlan):
                _mk(s, model, trip_id=trip.id, user_id=uid)
            _mk(s, m.DiscoveryItem, trip_id=trip.id, user_id=uid, finding_id="f")
            _mk(s, m.PackingItem, trip_id=trip.id, user_id=uid)
            _mk(s, m.JournalEntry, owner_id=uid, trip_id=trip.id, body="on the trip")
            _mk(s, m.TravelItem, owner_id=uid, trip_id=trip.id)
            doc = _mk(s, m.Document, owner_id=uid, trip_id=trip.id, filename="filed.pdf")
            p = documents.path_for(doc)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"%PDF filed")
            place = _mk(s, m.SavedPlace, owner_id=uid, name="Filed place")
            _mk(s, m.SavedPlaceFiling, owner_id=uid, place_id=place.id, trip_id=trip.id)
        for u in people:
            uid = u.id
            exp = _mk(s, m.TripExpense, trip_id=trip.id, owner_id=uid,
                      payer_id=u.traveler_id, title="Dinner")
            for other in people:
                _mk(s, m.TripExpenseShare, expense_id=exp.id, traveler_id=other.traveler_id)
                _mk(s, m.TripExpenseComment, expense_id=exp.id, trip_id=trip.id,
                    author_id=other.id, body="thanks")
            mine = _mk(s, m.TripBudgetItem, trip_id=trip.id, owner_id=uid, title="Just mine")
            _mk(s, m.TripBudgetTraveler, budget_item_id=mine.id, traveler_id=u.traveler_id)
            _mk(s, m.TripExpense, trip_id=trip.id, owner_id=uid, payer_id=u.traveler_id,
                budget_item_id=mine.id, title="Paid from the plan")
            ours = _mk(s, m.TripBudgetItem, trip_id=trip.id, owner_id=uid, title="For all")
            for other in people:
                _mk(s, m.TripBudgetTraveler, budget_item_id=ours.id,
                    traveler_id=other.traveler_id)
            _mk(s, m.TripRate, trip_id=trip.id, fetched_by=uid,
                currency=uuid.uuid4().hex[:3].upper())
        return trip

    class World:
        pass

    def _world(tmp) -> World:
        """A leaver and a survivor with a full set of data each, a viewer, an
        admin, and trips in every arrangement of SPEC-13 §3.4."""
        w = World()
        with SessionLocal() as s:
            w.admin = _user(s, "admin", admin=True)
            w.leaver = _user(s, "leaver")
            w.survivor = _user(s, "survivor")
            w.viewer = _user(s, "viewer")
            w.mine = _own_rows(s, w.leaver, tmp)
            w.theirs = _own_rows(s, w.survivor, tmp)
            _own_rows(s, w.viewer, tmp)
            w.guest = _mk(s, m.TravelerIdentity, kind="guest", generic=False,
                          display_name="Del Guest " + uuid.uuid4().hex[:6])
            w.solo = _trip(s, "solo", w.leaver, guests=[w.guest])
            w.handed = _trip(s, "handed", w.leaver, travellers=[w.survivor])
            w.doomed = _trip(s, "doomed", w.leaver, travellers=[w.survivor])
            w.unchosen = _trip(s, "unchosen", w.leaver, viewers=[w.viewer])
            w.guest_on = _trip(s, "theirs", w.survivor, travellers=[w.leaver])
            w.viewed = _trip(s, "viewed", w.survivor, viewers=[w.leaver])
            # A key of the leaver's that a batch of the survivor's still points
            # at, and an item of the survivor's cut from one of the leaver's
            # batches (neither should happen; the purge must cope with both).
            w.cross_batch = _mk(s, m.IngestBatch, api_key_id=w.mine["key"].id,
                                user_id=w.survivor.id, payload={})
            w.cross_item = _mk(s, m.TravelItem, owner_id=w.survivor.id,
                               batch_id=w.mine["batch"].id,
                               revision={"prior_item_id": w.mine["item"].id})
            # The admin acted on the leaver once; the leaver on the survivor.
            _mk(s, m.AccountEvent, user_id=w.leaver.id, actor_id=w.admin.id,
                action="email_set", detail="by an admin")
            _mk(s, m.AccountEvent, user_id=w.survivor.id, actor_id=w.leaver.id,
                action="traveller_linked", detail="7")
            s.commit()
            for name in ("admin", "leaver", "survivor", "viewer", "guest", "solo", "handed",
                         "doomed", "unchosen", "guest_on", "viewed"):
                s.refresh(getattr(w, name))
            w.ids = {k: getattr(w, k).id for k in ("admin", "leaver", "survivor", "viewer",
                                                   "guest", "solo", "handed", "doomed",
                                                   "unchosen", "guest_on", "viewed")}
            w.tid = w.leaver.traveler_id
            w.email = w.leaver.email
            w.mine_ids = {k: sa.inspect(v).identity[0] for k, v in w.mine.items()}
            s.expunge_all()
        return w

    def _choices(w):
        return {w.ids["handed"]: {"action": "hand", "to": w.ids["survivor"]},
                w.ids["doomed"]: {"action": "delete"}}

    def _request(w, choices="default", who="leaver"):
        """Ask for the leaver's deletion. By default with a choice for two of
        the shared trips, and the third ("unchosen") shared only AFTER the
        request, so it reaches the purge with no recorded choice (§3.4)."""
        late = choices == "default" and who == "leaver"
        with SessionLocal() as s:
            if late:
                s.query(m.TripMember).filter_by(trip_id=w.ids["unchosen"],
                                                user_id=w.ids["viewer"]).delete()
                s.commit()
            u = s.get(m.User, w.ids[who])
            rid = deletion.request(
                s, u, actor=u, choices=_choices(w) if choices == "default" else choices).id
            if late:
                _mk(s, m.TripMember, trip_id=w.ids["unchosen"], user_id=w.ids["viewer"],
                    role="viewer")
                s.commit()
            return rid

    def _make_due(uid):
        with SessionLocal() as s:
            u = s.get(m.User, uid)
            u.deletion_due_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            s.commit()

    def _delete(w, who="leaver"):
        _make_due(w.ids[who])
        return deletion.run_due(SessionLocal, mailbox=False)

    # ------------------------------------------------------------------ snapshots
    def _snapshot() -> dict:
        """Every row of every table, keyed by table and primary key."""
        out = {}
        with engine.connect() as conn:
            for name, table in m.Base.metadata.tables.items():
                pk = [c.name for c in table.primary_key.columns]
                rows = {}
                for row in conn.execute(sa.select(table)).mappings():
                    rows[tuple(str(row[c]) for c in pk)] = {
                        k: json.dumps(v, sort_keys=True, default=str) for k, v in row.items()}
                out[name] = rows
        return out

    def _web(user_or_id):
        with SessionLocal() as s:
            u = s.get(m.User, getattr(user_or_id, "id", user_or_id))
            token = authn.issue_web_token(u)
        c = TestClient(app, follow_redirects=False)
        c.cookies.set(settings.auth_cookie_name, token)
        return c

    def _confirmed(uid):
        c = _web(uid)
        r = c.post("/account/confirm", data={"password": PW, "next": "/account/delete"})
        assert r.status_code == 303 and r.headers["location"] == "/account/delete", r.text
        return c

    def _username(uid):
        with SessionLocal() as s:
            return s.get(m.User, uid).username

    # ------------------------------------------------------------------ §4.4 point 3
    def test_every_table_a_deletion_touches_has_a_step():
        assert ownership.problems(m.Base.metadata) == []
        assert account_purge.uncovered() == []
        assert set(account_purge._STEPS) <= set(ownership.REGISTRY)

    # ------------------------------------------------------------------ §4.4 point 1
    def test_the_survivor_test(env):
        """Delete one account. Every other row in the database is unchanged,
        except the changes SPEC-13 lists."""
        w = _world(env)
        L, S, V, tid = w.ids["leaver"], w.ids["survivor"], w.ids["viewer"], w.tid
        _request(w)
        before = _snapshot()
        assert _delete(w) == 1
        after = _snapshot()

        gone_trips = {w.ids["solo"], w.ids["doomed"]}
        user_links = {t: [l.column for l in e.links if l.holds == ownership.USER]
                      for t, e in ownership.REGISTRY.items()}
        acts = {pk[0] for pk, r in before["trip_activities"].items()
                if json.loads(r["trip_id"]) in gone_trips}
        exps = {pk[0] for pk, r in before["trip_expenses"].items()
                if json.loads(r["trip_id"]) in gone_trips}
        # Planned costs: every one on a deleted trip, and the ones the leaver
        # planned for themself alone on the trips that go on.
        for_whom: dict = {}
        for pk in before["trip_budget_travelers"]:
            for_whom.setdefault(pk[0], set()).add(pk[1])
        solo_plans = {pk[0] for pk, r in before["trip_budget_items"].items()
                      if json.loads(r["owner_id"]) == L and for_whom.get(pk[0]) == {str(tid)}}
        assert len(solo_plans) >= 4
        plans = solo_plans | {pk[0] for pk, r in before["trip_budget_items"].items()
                              if json.loads(r["trip_id"]) in gone_trips}
        my_items = {pk[0] for pk, r in before["travel_items"].items()
                    if json.loads(r["owner_id"]) == L}
        my_templates = {pk[0] for pk, r in before["packing_templates"].items()
                        if json.loads(r["user_id"]) == L}

        def may_go(table, pk, row):
            val = lambda c: json.loads(row[c]) if c in row else None   # noqa: E731
            if any(val(c) == L for c in user_links.get(table, ())):
                return True                                   # the leaver's own row
            if table == "users":
                return pk[0] == L
            if table == "trips":
                return pk[0] in gone_trips
            if val("trip_id") in gone_trips:
                return True                                   # went with a deleted trip
            return {
                "home_profiles": val("traveler_id") == tid,
                "packing_profiles": val("traveler_id") == tid,
                "discovery_profiles": val("traveler_id") == tid,
                "trip_activity_travelers": val("activity_id") in acts,
                "trip_expense_shares": val("expense_id") in exps,
                "trip_budget_travelers": val("budget_item_id") in plans,
                "trip_budget_items": pk[0] in plans,
                "item_keys": val("item_id") in my_items,
                "commit_attempts": val("item_id") in my_items,
                "packing_template_items": val("template_id") in my_templates,
                "artifacts": pk[0] == w.mine_ids["art"],
                "traveler_identities": pk[0] == str(w.ids["guest"]),
                "alert_states": pk[0].startswith(f"integration:{L}:"),
                # The viewer became the owner of the trip nobody chose for.
                "trip_members": val("trip_id") == w.ids["unchosen"] and val("user_id") == V,
            }.get(table, False)

        def may_change(table, pk, old, new):
            cols = {c for c in old if old[c] != new[c]}
            val = lambda c: json.loads(old[c])                # noqa: E731
            if table == "trips":                              # handed over (§3.4)
                heir = {w.ids["handed"]: S, w.ids["unchosen"]: V}.get(pk[0])
                return (heir and cols <= {"owner_id", "updated_at"}
                        and json.loads(new["owner_id"]) == heir)
            if table in ("trip_expenses", "trip_budget_items"):
                ok = cols <= {"owner_id", "budget_item_id", "updated_at"}
                if "owner_id" in cols:
                    ok = ok and val("owner_id") == L and json.loads(new["owner_id"]) is None
                if "budget_item_id" in cols:
                    ok = ok and val("budget_item_id") in plans
                return ok
            if table == "trip_rates":
                return cols == {"fetched_by"} and val("fetched_by") == L
            if table == "traveler_identities":                # the traveller, now a guest
                return pk[0] == str(tid) and cols <= {
                    "kind", "aliases", "nationality", "base_currency", "timezone", "notes",
                    "airtrail_user_id"}
            if table == "account_events":
                return (cols <= {"user_id", "detail", "actor_id"}
                        and all(val(c) == L for c in cols - {"detail"})
                        and ("detail" not in cols or val("user_id") == L))
            if table == "ai_calls":
                return cols == {"user_id"} and val("user_id") == L
            if table == "server_settings":
                return cols <= {"updated_by", "updated_at"} and val("updated_by") == L
            if table in ("notification_targets", "oauth_clients"):
                return cols == {"created_by"} and val("created_by") == L
            if table == "account_deletions":
                return val("user_id") == L
            if table in ("travel_items", "journal_entries"):
                # Deleting a trip unlinks other people's captures and entries;
                # the survivor's item cut from the leaver's batch loses the pointer.
                ok = cols <= {"trip_id", "batch_id", "revision", "updated_at"}
                if "trip_id" in cols:
                    ok = ok and val("trip_id") in gone_trips
                if cols & {"batch_id", "revision"}:
                    ok = ok and pk[0] == w.cross_item.id
                return ok
            if table == "api_keys":                           # revoked and unlinked
                return pk[0] == w.mine_ids["key"] and cols == {"user_id", "revoked_at"}
            return False

        problems = []
        for table in before:
            for pk, row in before[table].items():
                new = after[table].get(pk)
                if new is None:
                    if not may_go(table, pk, row):
                        problems.append(f"{table} {pk} was deleted")
                elif new != row and not may_change(table, pk, row, new):
                    changed = sorted(c for c in row if row[c] != new[c])
                    problems.append(f"{table} {pk} changed {changed}")
            for pk, row in after[table].items():
                if pk not in before[table]:
                    if not (table == "account_events"
                            and json.loads(row["action"]) == "account_deleted"
                            and json.loads(row["user_id"]) is None):
                        problems.append(f"{table} {pk} appeared")
        assert problems == [], "\n".join(problems[:40])

        # And the listed changes did happen.
        with SessionLocal() as s:
            assert s.get(m.User, L) is None
            assert s.get(m.Trip, w.ids["solo"]) is None and s.get(m.Trip, w.ids["doomed"]) is None
            assert s.get(m.Trip, w.ids["handed"]).owner_id == S
            assert s.get(m.Trip, w.ids["unchosen"]).owner_id == V       # never ownerless
            assert s.get(m.Trip, w.ids["guest_on"]).owner_id == S
            assert s.query(m.Trip).filter(m.Trip.owner_id.is_(None),
                                          m.Trip.name.like("Del %")).count() == 0
            me = s.get(m.TravelerIdentity, tid)
            assert (me.kind, me.aliases, me.nationality, me.base_currency, me.timezone,
                    me.notes, me.airtrail_user_id) == ("guest", [], None, None, None, None, None)
            assert me.display_name.startswith("Del Leaver")
            assert s.query(m.TripTraveler).filter_by(trip_id=w.ids["guest_on"],
                                                     traveler_id=tid).count() == 1
            assert s.get(m.TravelerIdentity, w.ids["guest"]) is None
            assert s.query(m.TripMember).filter_by(user_id=L).count() == 0
            assert s.query(m.TripExpenseComment).filter_by(author_id=L).count() == 0
            key = s.get(m.ApiKey, w.mine_ids["key"])
            assert key.user_id is None and key.revoked_at is not None
            assert s.get(m.TravelItem, w.cross_item.id).revision == {"prior_item_id": None}
            log = s.query(m.AccountEvent).filter_by(action="account_deleted").one()
            assert (log.user_id, log.actor_id, log.detail) == (None, None, "by the person")
            assert s.query(m.IngestBatch).filter_by(id=w.mine_ids["batch"]).count() == 0
        # The survivor's file on the deleted trip went with it, as on any trip
        # delete; their own files elsewhere are still there.
        assert documents.path_for(w.theirs["doc"]).is_file()

    # ------------------------------------------------------------------ §4.4 point 2
    def test_the_id_scan(env):
        """After the purge the leaver's user id is in no column of any table,
        JSON included, and in no folder name on disk. Nor is their email."""
        w = _world(env)
        L = w.ids["leaver"]
        _request(w)
        assert _delete(w) == 1
        for table, rows in _snapshot().items():
            for pk, row in rows.items():
                text = json.dumps(row)
                assert L not in text, f"{table} {pk} still holds the leaver's id"
                assert w.email not in text, f"{table} {pk} still holds the leaver's email"
        for root in (Path(settings.documents_dir), Path(settings.export_dir)):
            assert L not in {p.name for p in root.rglob("*")}
            assert (root / w.ids["survivor"]).is_dir()         # the survivor's folders stay
        # Deleting the heir and the admin later leaves no id of theirs either:
        # the records and the audit rows that named them are cleared.
        with SessionLocal() as s:
            admin, viewer = s.get(m.User, w.ids["admin"]), s.get(m.User, w.ids["viewer"])
            deletion.request(s, s.get(m.User, w.ids["survivor"]), actor=admin)
            deletion.request(s, viewer, actor=viewer, choices={})
        _make_due(w.ids["survivor"])
        _make_due(w.ids["viewer"])
        assert deletion.run_due(SessionLocal, mailbox=False) == 2
        with SessionLocal() as s:
            other = _user(s, "second-admin", admin=True)
            s.commit()
            deletion.request(s, s.get(m.User, w.ids["admin"]), actor=other)
        assert _delete(w, "admin") == 1
        text = json.dumps({t: list(rows.values()) for t, rows in _snapshot().items()})
        for who in ("survivor", "viewer", "admin"):
            assert w.ids[who] not in text, who

    def test_a_null_traveller_never_touches_the_servers_defaults(env):
        with SessionLocal() as s:
            _user(s, "keeper", admin=True)
            u = _user(s, "bare", traveller=False)
            defaults = [_mk(s, model, traveler_id=None)
                        for model in (m.HomeProfile, m.PackingProfile, m.DiscoveryProfile)]
            ids = [(type(d), d.id) for d in defaults]
            s.commit()
            deletion.request(s, u, actor=u, choices={})
            uid = u.id
        _make_due(uid)
        assert deletion.run_due(SessionLocal, mailbox=False) == 1
        with SessionLocal() as s:
            assert s.get(m.User, uid) is None
            assert all(s.get(model, pk) is not None for model, pk in ids)

    def test_a_traveller_no_trip_names_is_deleted(env):
        with SessionLocal() as s:
            _user(s, "keeper", admin=True)
            u = _user(s, "loner")
            uid, tid = u.id, u.traveler_id
            s.commit()
            deletion.request(s, u, actor=u, choices={})
        _make_due(uid)
        deletion.run_due(SessionLocal, mailbox=False)
        with SessionLocal() as s:
            assert s.get(m.TravelerIdentity, tid) is None

    # ------------------------------------------------------------------ the request (§3.3)
    def test_the_request_needs_a_fresh_confirmation_the_username_and_the_choices(env):
        w = _world(env)
        L = w.ids["leaver"]
        c = _web(L)
        page = c.get("/account/delete")
        assert page.status_code == 200 and "Confirm it" in page.text
        assert page.headers["cache-control"] == "no-store"
        r = c.post("/account/delete", data={"username": _username(L)})
        assert r.status_code == 303 and r.headers["location"].startswith("/account/confirm")
        c = _confirmed(L)
        assert "Del handed" in c.get("/account/delete").text       # the trips that need a choice
        assert c.post("/account/delete", data={"username": "someone-else"}).status_code == 400
        r = c.post("/account/delete", data={"username": _username(L)})
        assert r.status_code == 400 and "Choose what happens" in r.text
        # A hand-over to someone who isn't on that trip is no choice at all.
        bad = {"username": _username(L), f"trip_{w.ids['handed']}": f"hand:{w.ids['admin']}",
               f"trip_{w.ids['doomed']}": "delete", f"trip_{w.ids['unchosen']}": "delete"}
        assert c.post("/account/delete", data=bad).status_code == 400
        with SessionLocal() as s:
            assert s.get(m.User, L).deletion_due_at is None
        good = dict(bad, **{f"trip_{w.ids['handed']}": f"hand:{w.ids['survivor']}"})
        r = c.post("/account/delete", data=good)
        assert r.status_code == 303 and r.headers["location"] == "/account/delete"
        with SessionLocal() as s:
            u = s.get(m.User, L)
            rec = deletion.record(s, u)
            due = deletion._aware(u.deletion_due_at)
            assert timedelta(days=6, hours=23) < due - datetime.now(timezone.utc) <= timedelta(days=7)
            assert rec.choices[w.ids["handed"]] == {"action": "hand", "to": w.ids["survivor"]}
            assert rec.choices[w.ids["unchosen"]] == {"action": "delete"}
            assert (u.share_with_admins, u.token_version) == (False, 1)
            assert s.query(m.RefreshToken).filter_by(user_id=L, revoked_at=None).count() == 0
            assert s.query(m.AccountEvent).filter_by(user_id=L,
                                                     action="deletion_requested").count() == 1
        # The browser that asked is still signed in, and held to the one page.
        locked = c.get("/account/delete")
        assert locked.status_code == 200 and "locked and will be deleted" in locked.text
        assert c.get("/account").headers["location"] == "/account/delete"

    def test_nothing_changes_during_the_seven_days_and_cancel_undoes_it(env):
        w = _world(env)
        L = w.ids["leaver"]
        before = _snapshot()
        _request(w, choices=dict(_choices(w), **{w.ids["unchosen"]: {"action": "delete"}}))
        assert deletion.run_due(SessionLocal, mailbox=False) == 0     # not due yet
        c = _web(L)
        r = c.post("/account/delete/cancel")
        assert r.status_code == 303 and r.headers["location"].startswith("/account?msg=")
        after = _snapshot()
        allowed = {"users": {"deletion_due_at", "token_version", "share_with_admins",
                             "support_until"},
                   "refresh_tokens": {"revoked_at"}}
        for table in before:
            if table in ("account_deletions", "account_events"):
                continue
            assert set(before[table]) == set(after[table]), table
            for pk, row in before[table].items():
                changed = {col for col in row if row[col] != after[table][pk][col]}
                assert changed <= allowed.get(table, set()), (table, pk, changed)
        with SessionLocal() as s:
            u = s.get(m.User, L)
            assert u.deletion_due_at is None and deletion.record(s, u) is None
            rec = s.query(m.AccountDeletion).filter_by(user_id=L).one()
            assert (rec.status, rec.choices) == ("cancelled", None)
        assert _web(L).get("/account").status_code == 200

    # ------------------------------------------------------------------ §4.4 point 7: the lock
    def test_a_locked_account_is_refused_everywhere_but_one_page(env):
        w = _world(env)
        L = w.ids["leaver"]
        with SessionLocal() as s:
            u = s.get(m.User, L)
            refresh = authn.create_refresh_token(s, u)
            pat = oauth.PAT_PREFIX + uuid.uuid4().hex
            _mk(s, m.PersonalApiToken, user_id=L, token_hash=oauth._hash(pat),
                token_version=u.token_version + 1)
            raw_key = auth.generate_key()
            _mk(s, m.ApiKey, user_id=L, key_hash=auth.hash_key(raw_key))
            token = _mk(s, m.InboxAddress, user_id=w.ids["admin"]).token   # a control
            mine = s.query(m.InboxAddress).filter_by(user_id=L).one().token
            s.commit()
        _request(w)
        with SessionLocal() as s:
            u = s.get(m.User, L)
            access, web = authn.issue_access_token(u), authn.issue_web_token(u)
            username = u.username
        api = TestClient(app, follow_redirects=False)
        # The API issues no tokens, and says why only once the password is right.
        r = api.post("/api/v1/auth/login", json={"username": username, "password": PW})
        assert r.status_code == 403 and r.json()["deletion_pending"] is True
        r = api.post("/api/v1/auth/login", json={"username": username, "password": "wrong"})
        assert r.status_code == 401 and "deletion_pending" not in r.json()
        r = api.post("/api/v1/auth/refresh", json={"refresh": refresh})
        assert r.status_code == 403 and r.json()["deletion_pending"] is True
        # Tokens: an app token and a personal API token resolve to nobody.
        for bearer in (access, pat):
            r = api.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {bearer}"})
            assert r.status_code == 401, bearer[:6]
        with SessionLocal() as s:
            assert authn.user_from_token(s, access) is None
            assert authn.user_from_token(s, web, kinds=("web",)).id == L
            assert not oauth._user_ok(s.get(m.User, L), m.OAuthGrant(token_version=1))
        # The web session reaches one page, the export and the confirmation.
        c = _web(L)
        for path in ("/", "/account", "/trips", "/places", "/journal"):
            r = c.get(path)
            assert (r.status_code, r.headers["location"]) == (303, "/account/delete"), path
        r = c.get("/api/v1/trips")
        assert r.status_code == 403 and r.json()["deletion_pending"] is True
        assert c.get("/account/delete").status_code == 200
        assert c.get("/account/export").status_code == 200
        assert c.get("/account/confirm?next=/account/export").status_code == 200
        r = c.get("/oauth/authorize?client_id=x&response_type=code")
        assert r.status_code != 200 or "Allow" not in r.text
        # Ingest keys are refused.
        import time
        body, ts = b"{}", str(int(time.time()))
        r = api.post("/ingest", content=body, headers={
            "X-Api-Key": raw_key, "X-Timestamp": ts,
            "X-Signature": auth.sign(raw_key, ts, body)})
        assert r.status_code == 403, r.text
        # Mail to the forward address is dropped with its own outcome.
        domain = "inbox.example.test"
        raw = (f"Delivered-To: trips+{mine}@{domain}\r\nSubject: A booking\r\n"
               f"Message-ID: <{uuid.uuid4().hex}@example.test>\r\n\r\nhello").encode()
        with SessionLocal() as s:
            if inbox.recipient_tokens(__import__("email").message_from_bytes(raw)):
                out = inbox.handle_message(s, raw)
                assert [h.outcome for h in out] == [inbox.ACCOUNT_LOCKED]
                assert s.query(m.IngestBatch).filter_by(user_id=L).count() == 1   # no new capture
        assert token

    def test_inbox_routing_drops_mail_for_a_locked_account(env, monkeypatch):
        w = _world(env)
        L = w.ids["leaver"]
        _request(w)
        with SessionLocal() as s:
            mine = s.query(m.InboxAddress).filter_by(user_id=L).one().token
            monkeypatch.setattr(inbox, "recipient_tokens", lambda msg: [mine])
            out = inbox.handle_message(s, b"Subject: A booking\r\n\r\nhello")
            assert [h.outcome for h in out] == [inbox.ACCOUNT_LOCKED]
            row = s.query(m.InboxMessage).filter_by(outcome=inbox.ACCOUNT_LOCKED).one()
            assert row.user_id is None

    # ------------------------------------------------------------------ §4.4 point 6: scoping
    def test_nobody_else_can_ask_for_or_cancel_a_deletion(env):
        w = _world(env)
        L, S = w.ids["leaver"], w.ids["survivor"]
        # A non-admin can't delete anyone else.
        r = _web(S).post(f"/users/{L}/delete", data={"confirm_username": _username(L)})
        assert r.status_code == 403
        _request(w)
        # Another person's cancel acts on their own account only.
        assert _web(S).post("/account/delete/cancel").status_code == 400
        assert _web(S).post(f"/users/{L}/delete/cancel").status_code == 403
        with SessionLocal() as s:
            assert s.get(m.User, L).deletion_due_at is not None
        # No API, token or MCP path: a bearer credential is refused outright.
        with SessionLocal() as s:
            bearer = {"Authorization": "Bearer " + authn.issue_access_token(s.get(m.User, S))}
        api = TestClient(app, follow_redirects=False)
        assert api.get("/account/delete", headers=bearer).status_code == 403
        assert api.post("/account/delete", headers=bearer,
                        data={"username": _username(S)}).status_code == 403
        assert api.post("/api/v1/account/delete", headers=bearer).status_code in (404, 405)
        with SessionLocal() as s:
            assert s.get(m.User, S).deletion_due_at is None

    def test_an_admin_deletes_another_account_by_typing_its_username(env):
        w = _world(env)
        A, L = w.ids["admin"], w.ids["leaver"]
        c = _web(A)
        page = c.get("/users").text
        assert "Delete account" in page and "Del handed" not in page       # counts, never names
        with SessionLocal() as s:
            assert deletion.preview(s, s.get(m.User, L)) == {"handed": 3, "deleted": 1}
        r = c.post(f"/users/{L}/delete", data={"confirm_username": "wrong"})
        assert "err=" in r.headers["location"]
        r = c.post(f"/users/{A}/delete", data={"confirm_username": _username(A)})
        assert r.status_code == 400                                        # your own is yours
        r = c.post(f"/users/{L}/delete", data={"confirm_username": _username(L)})
        assert r.status_code == 303 and "msg=" in r.headers["location"]
        with SessionLocal() as s:
            rec = deletion.record(s, s.get(m.User, L))
            assert (rec.by_admin, rec.requested_by, rec.choices) == (True, A, None)
        # The person can't cancel an admin's deletion, but can still export.
        mine = _web(L)
        assert mine.post("/account/delete/cancel").status_code == 400
        assert "Only an administrator" in mine.get("/account/delete").text or \
            "only an administrator" in mine.get("/account/delete").text
        assert mine.get("/account/export").status_code == 200
        # With no choices, every shared trip is handed over, never deleted.
        assert _delete(w) == 1
        with SessionLocal() as s:
            assert {s.get(m.Trip, w.ids[t]).owner_id for t in ("handed", "doomed")} == {
                w.ids["survivor"]}
            assert s.get(m.Trip, w.ids["unchosen"]).owner_id == w.ids["viewer"]
            assert s.get(m.Trip, w.ids["solo"]) is None
            log = s.query(m.AccountEvent).filter_by(action="account_deleted").one()
            assert (log.user_id, log.actor_id, log.detail) == (None, A, "by an admin")

    def test_an_admin_can_cancel(env):
        w = _world(env)
        A, L = w.ids["admin"], w.ids["leaver"]
        c = _web(A)
        c.post(f"/users/{L}/delete", data={"confirm_username": _username(L)})
        assert c.post(f"/users/{L}/delete/cancel").status_code == 303
        with SessionLocal() as s:
            assert s.get(m.User, L).deletion_due_at is None

    # ------------------------------------------------------------------ §4.4 point 7: guards
    def test_the_last_admin_cant_be_deleted(env):
        w = _world(env)
        A = w.ids["admin"]
        c = _confirmed(A)
        assert "only active admin" in c.get("/account/delete").text
        r = c.post("/account/delete", data={"username": _username(A)})
        assert r.status_code == 400 and "only active admin" in r.text
        with SessionLocal() as s:
            assert s.get(m.User, A).deletion_due_at is None
            # A second admin who is deactivated, or already leaving, doesn't count.
            other = _user(s, "other-admin", admin=True)
            other.is_active = False
            s.commit()
            assert deletion.last_admin(s, s.get(m.User, A))
            other.is_active = True
            s.commit()
            assert not deletion.last_admin(s, s.get(m.User, A))
            deletion.request(s, other, actor=other, choices={})
            assert deletion.last_admin(s, s.get(m.User, A))
            with pytest.raises(deletion.Refused):
                deletion.request(s, s.get(m.User, A), actor=s.get(m.User, A), choices={})
        assert cli.main(["delete-account", _username(A), "--now"]) == 1
        with SessionLocal() as s:
            assert s.get(m.User, A) is not None

    def test_the_purge_checks_the_last_admin_again(env):
        """Two admins; one asks to leave; the other is deactivated during the
        7 days. The purge holds rather than leave the server without an admin."""
        with SessionLocal() as s:
            a, b = _user(s, "first", admin=True), _user(s, "second", admin=True)
            s.commit()
            deletion.request(s, a, actor=a, choices={})
            b.is_active = False
            s.commit()
            aid, bid = a.id, b.id
        _make_due(aid)
        assert deletion.run_due(SessionLocal, mailbox=False) == 0
        with SessionLocal() as s:
            assert s.get(m.User, aid).deletion_due_at is not None      # still pending
            s.get(m.User, bid).is_active = True
            s.commit()
        assert deletion.run_due(SessionLocal, mailbox=False) == 1

    def test_the_users_page_guards_ignore_an_admin_who_is_leaving(env):
        with SessionLocal() as s:
            a, b = _user(s, "first", admin=True), _user(s, "second", admin=True)
            s.commit()
            deletion.request(s, b, actor=b, choices={})
            aid, bid = a.id, b.id
        with SessionLocal() as s:
            c = _user(s, "third", admin=True)
            s.commit()
            cid = c.id
        web = _web(cid)
        # `first` is the only other usable admin besides `third`: fine to demote.
        assert "msg=" in web.post(f"/users/{aid}/admin").headers["location"]
        # Now `third` is the last usable one; a leaving admin doesn't count.
        with SessionLocal() as s:
            assert deletion.usable_admins(s) == 1
            assert deletion.last_admin(s, s.get(m.User, cid))
        assert bid

    def test_no_deletion_on_a_demo_server_or_with_sign_in_off(env, monkeypatch):
        w = _world(env)
        c = _web(w.ids["leaver"])
        monkeypatch.setattr(settings, "auth_required", False)
        assert c.get("/account/delete").status_code == 404
        assert deletion.run_due(SessionLocal) == 0
        monkeypatch.setattr(settings, "auth_required", True)
        for k, v in (("demo_mode", True), ("demo_username", "del-demo"), ("demo_password", "pw")):
            monkeypatch.setattr(settings, k, v)
        assert c.get("/account/delete").status_code == 404
        assert c.post("/account/delete", data={"username": "x"}).status_code in (403, 404)
        assert _web(w.ids["admin"]).post(
            f"/users/{w.ids['leaver']}/delete", data={"confirm_username": "x"}).status_code == 404

    # ------------------------------------------------------------------ §3.4: decided again
    def test_the_purge_decides_again_when_the_roster_changed(env):
        w = _world(env)
        L, S = w.ids["leaver"], w.ids["survivor"]
        _request(w)
        with SessionLocal() as s:
            # The chosen heir left the handed trip; a viewer was added meanwhile.
            s.query(m.TripTraveler).filter_by(
                trip_id=w.ids["handed"],
                traveler_id=s.get(m.User, S).traveler_id).delete()
            _mk(s, m.TripMember, trip_id=w.ids["handed"], user_id=w.ids["viewer"], role="viewer")
            # A trip that had nobody else on it now has someone: never deleted by default.
            _mk(s, m.TripMember, trip_id=w.ids["solo"], user_id=S, role="viewer")
            # The viewer of the unchosen trip is gone, so nobody is left on it.
            s.query(m.TripMember).filter_by(trip_id=w.ids["unchosen"]).delete()
            s.commit()
        assert _delete(w) == 1
        with SessionLocal() as s:
            assert s.get(m.Trip, w.ids["handed"]).owner_id == w.ids["viewer"]
            assert s.get(m.Trip, w.ids["solo"]).owner_id == S
            assert s.get(m.Trip, w.ids["unchosen"]) is None
            assert s.get(m.Trip, w.ids["doomed"]) is None               # marked "delete"

    def test_a_trip_goes_to_a_traveller_before_a_viewer_and_the_oldest_first(env):
        with SessionLocal() as s:
            owner = _user(s, "owner")
            old = _user(s, "old", created_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
            new = _user(s, "new", created_at=datetime(2030, 6, 1, tzinfo=timezone.utc))
            viewer = _user(s, "early-viewer", created_at=datetime(2029, 1, 1, tzinfo=timezone.utc))
            leaving = _user(s, "leaving", created_at=datetime(2028, 1, 1, tzinfo=timezone.utc))
            trip = _trip(s, "order", owner, travellers=[new, old, leaving], viewers=[viewer])
            leaving.deletion_due_at = datetime.now(timezone.utc) + timedelta(days=3)
            s.commit()
            order = [u.id for u in account_purge.accounts_on(s, trip, leaver_id=owner.id)]
            assert order == [old.id, new.id, viewer.id, leaving.id]

    # ------------------------------------------------------------------ the daily step
    def test_one_failing_purge_doesnt_hold_up_the_others(env, monkeypatch):
        with SessionLocal() as s:
            _user(s, "keeper", admin=True)
            a, b = _user(s, "a"), _user(s, "b")
            s.commit()
            for u in (a, b):
                deletion.request(s, u, actor=u, choices={})
            aid, bid = a.id, b.id
        _make_due(aid)
        _make_due(bid)
        real = account_purge.purge

        def flaky(session, user_id, **kw):
            if user_id == aid:
                raise RuntimeError("boom")
            return real(session, user_id, **kw)
        monkeypatch.setattr(account_purge, "purge", flaky)
        assert deletion.run_due(SessionLocal, mailbox=False) == 1
        monkeypatch.setattr(account_purge, "purge", real)
        with SessionLocal() as s:
            assert s.get(m.User, bid) is None
            assert s.get(m.User, aid).deletion_due_at is not None      # still pending, intact
        assert deletion.run_due(SessionLocal, mailbox=False) == 1      # finished the next day
        assert deletion.run_due(SessionLocal, mailbox=False) == 0      # and safe to run again

    def test_the_retention_job_runs_the_step(env):
        w = _world(env)
        _request(w)
        _make_due(w.ids["leaver"])
        report = retention.run(SessionLocal, mailbox=False)
        assert report.accounts_deleted == 1
        dry = retention.run(SessionLocal, dry_run=True, mailbox=False)
        assert dry.accounts_deleted == 0

    def test_leftover_folders_of_deleted_accounts_are_removed(env):
        w = _world(env)
        gone = str(uuid.uuid4())
        docs, exports = Path(settings.documents_dir), Path(settings.export_dir)
        for root in (docs, exports):
            (root / gone).mkdir(parents=True)
            (root / gone / "left").write_bytes(b"x")
            (root / "not-a-user-id").mkdir()
        assert deletion.remove_orphan_folders(SessionLocal) == 2
        for root in (docs, exports):
            assert not (root / gone).exists()
            assert (root / "not-a-user-id").is_dir()           # only user-id-shaped names
            assert (root / w.ids["survivor"]).is_dir()         # a living account's folder

    # ------------------------------------------------------------------ the mailbox (§3.5)
    class _Imap:
        """A mailbox with the Captured folder and a Trash."""
        def __init__(self, uids):
            self.uids, self.moved = uids, []

        def list(self):
            return "OK", [b'(\\HasNoChildren \\Trash) "/" "Trash"']

        def select(self, folder):
            return "OK", [b"1"]

        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                mid = args[-1].strip('"')
                return "OK", [self.uids.get(mid, b"")]
            self.moved.append(args[0])
            return "OK", [b""]

        def expunge(self):
            return "OK", []

        def logout(self):
            return "BYE", []

    def test_captured_mail_is_moved_to_trash_and_retried(env, monkeypatch):
        w = _world(env)
        _request(w)
        with SessionLocal() as s:
            mid = s.get(m.IngestBatch, w.mine_ids["batch"]).payload["message_id"]
        assert _delete(w) == 1
        with SessionLocal() as s:
            rec = s.query(m.AccountDeletion).filter_by(status="done").one()
            # Only the Message-IDs are kept, on a record with no user.
            assert (rec.user_id, rec.choices, rec.mail_ids) == (None, None, [mid])
            rid = rec.id
        monkeypatch.setattr(inbox, "configured", lambda: True)
        monkeypatch.setattr(inbox, "_move", lambda conn, uid, folder: conn.moved.append(uid) or True)
        monkeypatch.setattr(retention, "_trash_folder", lambda conn: "Trash")

        def down():
            raise OSError("unreachable")
        assert deletion.retry_mail(SessionLocal, connect=down) == 0
        with SessionLocal() as s:
            assert s.get(m.AccountDeletion, rid).mail_ids == [mid]      # kept for tomorrow
        box = _Imap({mid: b"7"})
        assert deletion.retry_mail(SessionLocal, connect=lambda: box) == 1
        assert box.moved == [b"7"]
        with SessionLocal() as s:
            assert s.get(m.AccountDeletion, rid).mail_ids is None

    def test_the_mailbox_retry_stops_after_thirty_days(env, monkeypatch):
        with SessionLocal() as s:
            rec = m.AccountDeletion(status="done", due_at=datetime.now(timezone.utc),
                                    finished_at=datetime.now(timezone.utc) - timedelta(days=31),
                                    mail_ids=["<x@example.test>"])
            s.add(rec)
            s.commit()
            rid = rec.id
        monkeypatch.setattr(inbox, "configured", lambda: True)

        def down():
            raise OSError("unreachable")
        deletion.retry_mail(SessionLocal, connect=down)
        with SessionLocal() as s:
            assert s.get(m.AccountDeletion, rid).mail_ids is None

    # ------------------------------------------------------------------ emails (§3.1, §3.4)
    def test_the_emails(env, monkeypatch):
        sent = []
        monkeypatch.setattr(deletion, "email_configured", lambda: True)
        monkeypatch.setattr(deletion, "send_email",
                            lambda to, subject, body: sent.append((to, subject, body)) or True)
        w = _world(env)
        with SessionLocal() as s:
            mail = {k: s.get(m.User, w.ids[k]).email for k in ("leaver", "survivor", "viewer")}
        _request(w, choices=dict(_choices(w), **{w.ids["unchosen"]: {"action": "delete"}}))
        to = [t for t, _, _ in sent]
        assert to.count(mail["leaver"]) == 1
        assert to.count(mail["survivor"]) == 2          # one trip handed, one deleted
        assert to.count(mail["viewer"]) == 1
        assert all(w.ids["leaver"] not in body and "http" not in body for _, _, body in sent)
        handed = [b for t, _, b in sent if t == mail["survivor"] and "become yours" in b]
        assert len(handed) == 1 and "Del handed" in handed[0]
        sent.clear()
        with SessionLocal() as s:
            u = s.get(m.User, w.ids["leaver"])
            deletion.cancel(s, u, actor=u)
        assert sorted(t for t, _, _ in sent) == sorted(
            [mail["leaver"], mail["survivor"], mail["survivor"], mail["viewer"]])
        sent.clear()
        _request(w)
        sent.clear()
        assert _delete(w) == 1
        assert [(t, s) for t, s, _ in sent] == [
            (mail["leaver"], "Your Run·dere account was deleted")]

    def test_a_failing_mail_server_never_stops_a_deletion(env, monkeypatch):
        def broken(*a):
            raise OSError("smtp down")
        monkeypatch.setattr(deletion, "email_configured", lambda: True)
        monkeypatch.setattr(deletion, "send_email", broken)
        w = _world(env)
        _request(w)
        assert _delete(w) == 1

    # ------------------------------------------------------------------ the command line (§3.6)
    def test_the_command_line(env, capsys):
        w = _world(env)
        name = _username(w.ids["leaver"])
        before = _snapshot()
        assert cli.main(["delete-account", name, "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "nothing was changed" in out and "journal_entries" in out
        assert _snapshot() == before                                    # the real path, rolled back
        assert documents.path_for(w.mine["doc"]).is_file()
        assert cli.main(["delete-account", name]) == 0
        with SessionLocal() as s:
            rec = deletion.record(s, s.get(m.User, w.ids["leaver"]))
            assert (rec.by_admin, rec.requested_by) == (True, None)
        assert cli.main(["delete-account", name]) == 1                  # already waiting
        assert cli.main(["delete-account", name, "--cancel"]) == 0
        with SessionLocal() as s:
            assert s.get(m.User, w.ids["leaver"]).deletion_due_at is None
        assert cli.main(["delete-account", name, "--now"]) == 0
        with SessionLocal() as s:
            assert s.get(m.User, w.ids["leaver"]) is None
            # No choices on the command line: shared trips are handed over.
            assert s.get(m.Trip, w.ids["doomed"]).owner_id == w.ids["survivor"]
        assert not documents.path_for(w.mine["doc"]).exists()
        assert cli.main(["delete-account", "del-nobody"]) == 1

    def test_the_account_page_links_to_deletion(env):
        w = _world(env)
        assert 'href="/account/delete"' in _web(w.ids["survivor"]).get("/account").text
