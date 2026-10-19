"""Security review, area 8: the image (YONDER-293). Building it is CI's job;
these pin what the Dockerfile and entrypoint promise."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
ENTRY = ROOT / "docker-entrypoint.sh"


def test_image_installs_production_dependencies_only():
    assert "pip install --no-cache-dir .\n" in DOCKERFILE
    assert ".[dev]" not in DOCKERFILE


def test_app_runs_unprivileged_without_a_server_header():
    assert "useradd --system --uid 10001" in DOCKERFILE
    assert 'ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]' in DOCKERFILE
    assert "--no-server-header" in DOCKERFILE
    assert "HEALTHCHECK" in DOCKERFILE
    text = ENTRY.read_text()
    assert "exec setpriv --reuid=\"$APP_ID\" --regid=\"$APP_ID\" --clear-groups" in text
    assert "umask 027" in text


def test_entrypoint_is_valid_sh():
    assert subprocess.run(["sh", "-n", str(ENTRY)]).returncode == 0


def test_any_uid_gets_a_writable_home(tmp_path):
    """Run as the host's own uid (user: "1000:1000"), the image's home belongs
    to 10001: the entrypoint points HOME somewhere writable instead."""
    import os
    if os.geteuid() == 0:
        return                                  # the root path is the other branch
    out = subprocess.run(["sh", str(ENTRY), "sh", "-c", 'echo "$HOME"; test -w "$HOME"'],
                         env={**os.environ, "HOME": "/nonexistent-home"},
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().startswith("/tmp/rundere-home-")


def test_image_checks_data_paths_before_migrating():
    """A read-only data path stops start-up with the paths and the fix, before
    alembic could crash on it with a traceback (the 4.12.9 demo incident)."""
    assert '"python -m app.storage && alembic upgrade head && uvicorn' in DOCKERFILE
