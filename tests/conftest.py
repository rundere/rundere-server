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
# No live road routing in tests (resolve/routing.py); tests stub it.
os.environ.setdefault("TX_OSRM_URL", "")
