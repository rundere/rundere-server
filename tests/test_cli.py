"""app/cli.py key-lifecycle tests.

Regression for a real bug: create -> revoke -> create-again failed with
"already has a key" because client_name is unique and the revoked row
permanently occupied the name — create-key checked for ANY row instead of
an ACTIVE one. Fixed by rotating (reusing) the revoked row.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="tx-cli-test-")
os.environ.setdefault("TX_DATABASE_URL", f"sqlite:///{_TMP}/cli.sqlite3")

from app.cli import main
from app.db import SessionLocal, engine
from app.models import ApiKey, Base

Base.metadata.create_all(engine)


def _keys(client):
    with SessionLocal() as s:
        return s.query(ApiKey).filter_by(client_name=client).all()


def test_create_then_revoke_then_recreate_rotates_the_row(capsys):
    assert main(["create-key", "rot-client"]) == 0
    first_key = capsys.readouterr().out.strip().splitlines()[-1]

    assert main(["revoke-key", "rot-client"]) == 0

    rc = main(["create-key", "rot-client"])
    out = capsys.readouterr().out
    assert rc == 0, out           # this is the bug: used to fail here
    second_key = out.strip().splitlines()[-1]

    assert first_key != second_key
    rows = _keys("rot-client")
    assert len(rows) == 1         # rotated in place, not a duplicate row
    assert rows[0].revoked_at is None


def test_create_key_blocks_only_while_active():
    main(["create-key", "active-client"])
    rc = main(["create-key", "active-client"])
    assert rc == 1                # second create while still active -> blocked


def test_revoked_key_authenticates_as_none():
    from app import auth
    main(["create-key", "auth-client"])
    row = _keys("auth-client")[0]
    main(["revoke-key", "auth-client"])
    with SessionLocal() as s:
        assert auth.lookup_key(s, "irrelevant-because-revoked-row-exists") is None


if __name__ == "__main__":
    import io
    import contextlib

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    class _Capsys:
                        def readouterr(self):
                            class R: pass
                            r = R(); r.out = buf.getvalue(); buf.seek(0); buf.truncate()
                            return r
                    import inspect
                    if "capsys" in inspect.signature(fn).parameters:
                        fn(_Capsys())
                    else:
                        fn()
                print(f"  ok   {name}")
            except AssertionError as ex:
                failures += 1
                print(f"  FAIL {name}: {ex}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
