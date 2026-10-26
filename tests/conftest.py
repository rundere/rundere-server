"""Session-wide test env, loaded by pytest BEFORE any test module is imported.

Why this exists: app.config.settings is a singleton built at first import of
app.config. Individual test files setting os.environ afterward has no effect
once a different test file (imported earlier, e.g. alphabetically) has
already triggered that first import. Centralizing here removes the ordering
dependency entirely.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="tx-tests-")
os.environ.setdefault("TX_DATABASE_URL", f"sqlite:///{_TMP}/tests.sqlite3")
os.environ.setdefault("TX_INGEST_RATE_LIMIT", "5")
# Captures must not start background extraction in unrelated tests; the auto-process
# tests turn it on themselves (D-158).
os.environ.setdefault("TX_AUTO_PROCESS", "false")
# Sign-in is on by default (D-203); the suite starts with it off and the auth
# tests turn it on themselves, as they always have.
os.environ.setdefault("TX_AUTH_REQUIRED", "false")
# No live road routing in tests (resolve/routing.py); tests stub it.
os.environ.setdefault("TX_OSRM_URL", "")
# No live exchange rates (app/rates.py, D-195); tests stub rates.fetch.
os.environ.setdefault("TX_FRANKFURTER_URL", "")
# …nor live map tiles (app/trips/maps.py draws a schematic background instead).
os.environ.setdefault("TX_MAP_TILE_URL", "")

import pytest  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "email_gate: keep the local-account email check on (D-220)")


@pytest.fixture(autouse=True)
def _email_gate_off(request):
    """A local account without an email is asked for one at every web visit
    (D-220). Most tests make such accounts as fixtures, so the suite starts with
    that check off, like sign-in above; tests marked `email_gate` keep it on.

    Patched by hand rather than with `monkeypatch`: requesting that fixture here
    would make every test's own monkeypatch undo run after its other fixtures'
    teardown, and settings a fixture restores would be overwritten."""
    if request.node.get_closest_marker("email_gate"):
        yield
        return
    try:
        from app import authn
    except ImportError:                         # bare host without the app's deps
        yield
        return
    real = authn.needs_email
    authn.needs_email = lambda user: False
    try:
        yield
    finally:
        authn.needs_email = real
