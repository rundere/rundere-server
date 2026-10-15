"""YONDER-287: the pre-publish check. Fake secrets are assembled at run time, so
this file never trips the check it tests."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("prepublish", ROOT / "scripts" / "prepublish.py")
pp = importlib.util.module_from_spec(_spec)
sys.modules["prepublish"] = pp
_spec.loader.exec_module(pp)

GH = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"
RANDOM = "Zq8" + "vN2xLp4Rt7Yw9Kd3Hs6Fj1Gm5Bc0"     # 31 chars, high entropy


def _scan(text, strings=(), regexes=()):
    return pp.Scanner(list(strings), list(regexes)).scan_text(text, "x.py", "tree")


def test_token_formats_block_and_are_masked():
    found = _scan(f"token = '{GH}'\n{KEY_HEADER}\n")
    rules = {f.rule for f in found if f.check == "secrets"}
    assert {"github-token", "private-key"} <= rules
    assert all(f.severity == pp.BLOCK for f in found if f.check == "secrets")
    report = pp.report(found, history=False, denylist_on=True)
    assert GH not in report and GH[:2] + "…" in report      # masked, never whole


def test_assigned_secret_needs_entropy_and_skips_placeholders():
    assert any(f.rule == "assigned-secret" for f in _scan(f'API_KEY = "{RANDOM}"'))
    assert not _scan('API_KEY = "CHANGE_ME_DEMO_JWT_SECRET_VALUE"')
    assert not _scan('password = "aaaaaaaaaaaaaaaaaaaaaaaa"')        # no entropy
    assert not _scan(f'secret = "example-{RANDOM}"')                   # labelled example
    assert not _scan("password = settings.demo_password_value")          # code, not a value
    assert not _scan("token = auth.issue_web_token_for(user)")
    assert not _scan('ALPHABET_TOKEN = "abcdefghijklmnopqrstuvwxyz0123456789"')


def test_denylist_blocks_case_insensitively_and_in_paths(monkeypatch):
    s = pp.Scanner(["Jane Q Private"], [])
    found = s.scan_text("by jane q private\n", "docs/a.md", "tree")
    assert [f.severity for f in found if f.check == "denylist"] == [pp.BLOCK]
    assert s.scan_name("notes/jane q private/x.md", "tree")
    monkeypatch.setenv("PREPUBLISH_DENYLIST", '[deny]\nstrings = ["needle"]\nregexes = ["10\\\\.9\\\\.\\\\d+\\\\.\\\\d+"]')
    strings, regexes = pp.load_denylist(Path("/nonexistent"))
    assert strings == ["needle"] and regexes[0].search("host 10.9.1.2")


def test_placeholder_credentials_in_urls_are_skipped():
    assert not _scan("postgresql://travel:CHANGE_ME_DB_PASSWORD@db:5432/travel")
    assert any(f.rule == "credentials-in-url" for f in _scan(f"postgresql://travel:{RANDOM}@db/x"))


def test_infra_and_email_are_warnings():
    found = _scan("server.home.example 192.168.1.20 /home/someone me@gmail.com")
    assert {f.rule for f in found} == {"home-local-host", "private-ipv4", "home-path", "personal-email"}
    assert {f.severity for f in found} == {pp.WARN}
    assert not _scan("version 10.2.3.4.5 and 1.192.168.1")      # not addresses


def test_forbidden_files():
    for bad in ("backup.sql", "data/app.sqlite3", ".env", "home-profiles.local.json", ".claude/launch.json",
                "keys/server.pem"):
        assert pp.forbidden_file(bad), bad
    for ok in (".env.example", "docs/prepublish.local.example.toml", "app/main.py"):
        assert not pp.forbidden_file(ok), ok


def test_allowlist_needs_a_reason(tmp_path):
    f = tmp_path / "allow.toml"
    f.write_text('[[allow]]\nrule = "private-ipv4"\n')
    with pytest.raises(pp.ConfigError):
        pp.load_allowlist(f)
    f.write_text('[[allow]]\nrule = "private-ipv4"\npath = "app/*.py"\nreason = "SSRF policy ranges"\n')
    allow = pp.load_allowlist(f)
    hit = pp.Finding("infra", "private-ipv4", pp.WARN, "app/net.py")
    miss = pp.Finding("infra", "private-ipv4", pp.WARN, "docs/net.md")
    assert pp.is_allowed(hit, allow) == "SSRF policy ranges" and not pp.is_allowed(miss, allow)


def test_decision_patterns():
    assert pp.D_DEFINE.findall("- **D-201 — dark by default") == ["201"]
    assert pp.D_RANGE.findall("## D-110–D-116 — see SPEC-4") == [("110", "116")]
    assert pp.D_CITE.findall("per D-15 and D-198.") == ["15", "198"]


def test_the_repo_itself_has_no_blocking_secrets():
    # The current tree must never carry a real token format or private key.
    found = pp.check_tree(pp.Scanner([], []))
    # The judgement calls (assigned values, URLs with a password) go through the
    # reviewed report and allowlist instead.
    leaks = [f for f in found if f.check == "secrets"
             and f.rule not in ("assigned-secret", "credentials-in-url")]
    assert not leaks, [(f.rule, f.path, f.line) for f in leaks]
