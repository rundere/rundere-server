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
