"""Strict CSP: script-src 'self' (YONDER-303).

The pages run only the server's own static scripts, so a template must never
bring back an inline <script> block or an on*= handler: the browser would
refuse to run it, and the page would quietly break. JSON data blocks
(type="application/json") are fine; they never run. Behaviour goes in
app/review/static/rundere.js (data- attributes) or a page's own static file.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

APP = Path(__file__).resolve().parent.parent / "app"
TEMPLATES = sorted(APP.rglob("*.html"))

_SCRIPT = re.compile(r"<script\b([^>]*)>", re.I)
_DATA_TYPES = ("application/json", "application/ld+json")
_TAG = re.compile(r"<[a-zA-Z][^<>]*>", re.S)
_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)


def _rel(p: Path) -> str:
    return str(p.relative_to(APP.parent))


def _without_comments(text: str) -> str:
    # Jinja comments can mention <script> or on…= without being markup.
    return re.sub(r"\{#.*?#\}", "", text, flags=re.S)


def _inline_scripts(text: str) -> list[str]:
    text = _without_comments(text)
    bad = []
    for m in _SCRIPT.finditer(text):
        attrs = m.group(1)
        if re.search(r"\ssrc\s*=", " " + attrs, re.I):
            continue
        if any(t in attrs for t in _DATA_TYPES):
            continue
        bad.append(m.group(0))
    return bad


def _handlers(text: str) -> list[str]:
    text = _without_comments(text)        # and only real tags count
    return [t[:80] for t in _TAG.findall(text) if _HANDLER.search(t)]


def test_templates_were_found():
    assert len(TEMPLATES) > 80


@pytest.mark.parametrize("path", TEMPLATES, ids=_rel)
def test_no_inline_script_blocks(path):
    assert _inline_scripts(path.read_text(encoding="utf-8")) == [], (
        "move it to a static file (see app/review/static/rundere.js)")


@pytest.mark.parametrize("path", TEMPLATES, ids=_rel)
def test_no_inline_event_handlers(path):
    assert _handlers(path.read_text(encoding="utf-8")) == [], (
        "use a data- attribute (see app/review/static/rundere.js)")


def test_no_javascript_urls_in_links():
    for p in TEMPLATES:
        assert not re.search(r"href\s*=\s*[\"']javascript:", p.read_text(encoding="utf-8"),
                             re.I), _rel(p)


# ------------------------------------------------------------- the checker itself
def test_checker_catches_what_it_should():
    assert _inline_scripts("<script>alert(1)</script>")
    assert _inline_scripts('<script type="module">x()</script>')
    assert not _inline_scripts('<script src="/review/static/rundere.js" defer></script>')
    assert not _inline_scripts('<script type="application/json" id="r">{}</script>')
    assert _handlers('<button onclick="x()">')
    assert _handlers('<img src="a.png"\n     onerror="this.remove()">')
    assert not _handlers('<button data-action="tab">')
    assert not _handlers("<p>Click on = go</p>")
    assert not _handlers("{# onclick= was here #}<b>")
    assert not _inline_scripts("{# not a <script> block #}")


# ------------------------------------------------------------- the header
try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:                                  # pragma: no cover
    DEPS = False


@pytest.mark.skipif(not DEPS, reason="needs the app's dependencies")
def test_pages_get_script_src_self_only():
    from app.main import app
    client = TestClient(app)
    for path in ("/login", "/about", "/health"):
        csp = client.get(path).headers["content-security-policy"]
        assert "script-src 'self';" in csp, path
        assert "unsafe-inline" not in csp.split("style-src")[0], path
        assert "unsafe-eval" not in csp, path


@pytest.mark.skipif(not DEPS, reason="needs the app's dependencies")
def test_only_admin_keeps_inline_scripts():
    from app.main import _csp_for
    assert "script-src 'self' 'unsafe-inline'" in _csp_for("/admin")
    assert "script-src 'self' 'unsafe-inline'" in _csp_for("/admin/user/list")
    for path in ("/administrator", "/adminx", "/trips", "/"):
        assert "script-src 'self';" in _csp_for(path), path


@pytest.mark.skipif(not DEPS, reason="needs the app's dependencies")
def test_htmx_never_runs_scripts_in_swapped_content():
    for base in ("review/templates/base.html", "trips/templates/base.html"):
        text = (APP / base).read_text(encoding="utf-8")
        assert '"allowScriptTags":false' in text and '"allowEval":false' in text, base
