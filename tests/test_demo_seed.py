"""D-198 (YONDER-181): the demo server's sample data and its nightly roll-forward.
The seed writes three trips (under way, upcoming, past) through the app's own
paths without touching the network; the roll moves every date so they stay
current and is a no-op when they already are; the demo shows it all read-only."""
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    import pytest

    from app import authn, cli, demo, demo_seed, scope
    from app import models as m
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app

    m.Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)
    TODAY = date.today()

    @pytest.fixture()
    def seeded(monkeypatch):
        """A demo server's sample data for one test, written with the network cut."""
        import urllib.request

        def no_network(*a, **k):
            raise AssertionError("the demo seed must not touch the network")

        for k, v in (("demo_mode", True), ("demo_username", "demo-seed-t"),
                     ("demo_password", "pw"), ("auth_required", True)):
            monkeypatch.setattr(settings, k, v)
        with SessionLocal() as s:
            from planning.catalog import seed_catalog
            seed_catalog(s)
            demo.ensure_demo_user(s)
            with monkeypatch.context() as mp:
                mp.setattr(urllib.request, "urlopen", no_network)
                out = demo_seed.seed(s, replace=True, today=TODAY)
            uid = demo_seed._demo_user(s).id
        try:
            yield out, uid
        finally:
            with SessionLocal() as s:
                demo_seed._remove(s, s.get(m.User, uid))

    def _trips(s):
        return {t.external_ref: t for t in demo_seed.seeded_trips(s)}

    def _bearer(uid):
        with SessionLocal() as s:
            return {"Authorization": f"Bearer {authn.issue_access_token(s.get(m.User, uid))}"}

    # ------------------------------------------------------------------ seed
    def test_the_seed_writes_three_trips_around_today(seeded):
        out, uid = seeded
        assert out["seeded"] is True
        with SessionLocal() as s:
            t = _trips(s)
            lis, banff, nyc = t["demo:lisbon"], t["demo:banff"], t["demo:nyc"]
            d = lambda n: (TODAY + timedelta(days=n)).isoformat()
            assert (lis.start_date, lis.end_date) == (d(-1), d(3))          # under way
            assert banff.start_date == d(21) and nyc.end_date == d(-58)
            assert all(x.owner_id == uid for x in t.values())
            topics = {f.topic for f in s.query(m.DiscoveryFinding).filter_by(trip_id=lis.id)}
            assert {"weather", "currency", "plugs", "language", "food", "events",
                    "safety", "language_local"} <= topics
            # Home (Toronto) is never researched.
            assert not s.query(m.DiscoveryFinding).filter(
                m.DiscoveryFinding.place_key.like("%Toronto%")).count()
            assert s.query(m.DiscoveryItem).filter_by(trip_id=lis.id).count() >= 5
            assert s.query(m.PackingItem).filter_by(trip_id=banff.id, user_id=uid).count()
            assert s.query(m.TripExpense).filter_by(trip_id=nyc.id).count() == 4
            assert s.query(m.TripBudgetItem).filter_by(trip_id=nyc.id).count() == 3
            assert s.query(m.JournalEntry).filter_by(owner_id=uid).count() == 4
            caps = s.query(m.TravelItem).filter_by(owner_id=uid).all()
            assert sorted(c.item_type for c in caps) == ["flight", "lodging"]
            # The drive into Banff carries its route line, no router needed.
            drive = next(a for a in s.query(m.TripActivity).filter_by(trip_id=banff.id)
                         if a.type == "drive")
            assert drive.geo["route"]["polyline"]
            # Seeding again does nothing unless asked to replace.
            assert demo_seed.seed(s, today=TODAY)["seeded"] is False

    def test_the_seed_doesnt_leave_editing_on(seeded):
        _, uid = seeded
        with SessionLocal() as s:
            trip = _trips(s)["demo:lisbon"]
            assert not scope.can_edit_trip(s, s.get(m.User, uid), trip)

    # ------------------------------------------------------------------ roll
    def test_the_roll_moves_every_date_and_is_idempotent(seeded):
        _, uid = seeded
        later = TODAY + timedelta(days=3)
        with SessionLocal() as s:
            lis = _trips(s)["demo:lisbon"]
            before_act = sorted(a.starts_at for a in
                                s.query(m.TripActivity).filter_by(trip_id=lis.id))
            before_journal = sorted(j.entry_date for j in
                                    s.query(m.JournalEntry).filter_by(owner_id=uid))
            assert demo_seed.drift(s, TODAY) == 0 and demo_seed.drift(s, later) == 3
            assert demo_seed.roll(s, later) == {"rolled": 3}
            s.expire_all()
            lis = _trips(s)["demo:lisbon"]
            assert lis.start_date == (later - timedelta(days=1)).isoformat()
            after_act = sorted(a.starts_at for a in
                               s.query(m.TripActivity).filter_by(trip_id=lis.id))
            shift = lambda v: (date.fromisoformat(v[:10]) + timedelta(days=3)).isoformat() + v[10:]
            assert after_act == [shift(v) for v in before_act]           # times kept
            a = s.query(m.TripActivity).filter_by(trip_id=lis.id).first()
            assert a.values["when"] == a.starts_at
            assert sorted(j.entry_date for j in s.query(m.JournalEntry).filter_by(
                owner_id=uid)) == [shift(v) for v in before_journal]
            # The waiting captures are rebuilt, not doubled.
            assert s.query(m.TravelItem).filter_by(owner_id=uid).count() == 2
            # Again the same day: nothing to do.
            assert demo_seed.roll(s, later) == {"rolled": 0}

    def test_shift_keeps_times_and_ignores_non_dates():
        assert demo_seed._shift("2031-02-27T09:30", 2) == "2031-03-01T09:30"
        assert demo_seed._shift("2031-02-27", -1) == "2031-02-26"
        assert demo_seed._shift(None, 3) is None and demo_seed._shift("soon", 3) == "soon"

    # ------------------------------------------------------------------ shown read-only
    def test_the_demo_shows_the_sample_trips_read_only(seeded):
        _, uid = seeded
        h = _bearer(uid)
        with SessionLocal() as s:
            lis = _trips(s)["demo:lisbon"]
        d = client.get("/api/v1/dashboard", headers=h).json()
        assert d["next_activity"] and d["in_progress"]        # Today has a next moment and a stay
        snap = client.get(f"/api/v1/trips/{lis.id}", headers=h).json()
        assert (snap["trip"]["my_role"], snap["trip"]["can_edit"]) == ("owner", False)
        assert snap["places"] and snap["discovery"] and snap["discovery_items"]
        page = client.get(f"/trips/{lis.id}", headers=h).text
        assert "Pastéis de Belém" in page and "Casa do Miradouro" in page
        assert "+ Add activity" not in page and "Run discovery" not in page
        r = client.post(f"/api/v1/trips/{lis.id}/activities",
                        json={"type": "note", "values": {"title": "x"}}, headers=h)
        assert r.status_code == 403

    # ------------------------------------------------------------------ cli
    def test_the_cli_refuses_outside_a_demo_server(capsys):
        assert settings.demo_mode is False
        assert cli.main(["demo-seed"]) == 1
        assert "not a demo server" in capsys.readouterr().out
