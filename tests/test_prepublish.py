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
    assert strings == ["needle"] and regexes[0].search("host " + "10.9." + "1.2")


def test_placeholder_credentials_in_urls_are_skipped():
    assert not _scan("postgresql://travel:CHANGE_ME_DB_PASSWORD@db:5432/travel")
    assert not _scan("postgresql://travel:devonly-example@db:5432/travel")
    assert not _scan('TX_JWT_SECRET: "dev-4.0-test-stack-secret-not-for-prod"')
    url = "postgresql://travel:" + RANDOM + "@db/x"          # assembled, so this file stays clean
    assert any(f.rule == "credentials-in-url" for f in _scan(url))


def test_infra_and_email_are_warnings():
    found = _scan("server.home" + ".local " + "192.168." + "1.20 " + "/home" + "/someone " + "me@" + "gmail.com")
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
    def defined(text):
        return [a or b for a, b in pp.D_DEFINE.findall(text)]
    assert defined("- **D-201 — dark by default") == ["201"]
    assert defined("## D-01 — Standalone FastAPI app") == ["1"]          # heading, zero-padded
    assert defined("## D-110–D-116 — see SPEC-4") == []                  # a range is handled apart
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


def test_encoded_copies_are_decoded():
    import base64
    s = pp.Scanner(["needle"], [])
    hidden = base64.b64encode(b"contact needle@example.com today").decode()
    assert [f.rule for f in s.scan_text("x " + hidden, "a.md", "tree")] == ["deny-string"]
    found = s.scan_text("q=nee%64le", "a.md", "tree")          # %64 = d
    assert any("url-encoded" in f.detail for f in found)
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"   # binary: ignored
    assert not s.scan_text(png, "a.md", "tree")


def test_personal_formats_and_public_ips():
    def rules(t):
        return {f.rule for f in _scan(t)}
    assert rules("PNR: " + "XK7" + "Q2M") == {"booking-reference"}
    assert rules("e-ticket no. " + "014236" + "5789012") == {"ticket-number"}
    # Sample values are assembled so this file never appears in the check's own report.
    assert rules("call " + "+44 20 " + "7946 0958") == {"phone-number"}   # an Ofcom drama number
    assert rules("server at " + "52.10." + "20.30") == {"public-ipv4"}
    assert not rules("resolver 9.9.9.9")                     # a named public service
    assert not rules("see RFC 6749 section 4.1.2.1")         # a section number
    assert not rules("pypdf>=6.19.0")
    assert not pp.Scanner([], []).scan_text("x " + "52.10." + "20.30", "uv.lock", "tree")   # lockfile versions


DENY_EXAMPLE = """
[deny]
strings = ["Fred", "me@home.example"]
[rewrite]
"Fred" = "Alex"
"me@home.example" = "owner@example.com"
[drop]
paths = ["NOTES.md"]
"""


def test_rewrite_rules_keep_case_and_stay_outside_the_repo(tmp_path):
    written = pp.write_rewrite_rules(tmp_path / "rules", DENY_EXAMPLE)
    text = (tmp_path / "rules" / "replace-text.txt").read_text()
    assert "literal:FRED==>ALEX" in text and "literal:Fred==>Alex" in text and "literal:fred==>alex" in text
    assert "owner@example.com" in text and "NOTES.md" in (tmp_path / "rules" / "drop-paths.txt").read_text()
    assert "Merge PR" in (tmp_path / "rules" / "replace-message.txt").read_text()
    assert "mailmap" not in written                        # no [identity] given
    with pytest.raises(pp.ConfigError):
        pp.write_rewrite_rules(pp.ROOT / "rules-here", DENY_EXAMPLE)


def test_doc_links_and_naming_run_on_the_repo():
    assert isinstance(pp.check_doc_links(), list)
    names = pp.check_naming()
    assert all(f.check == "naming" for f in names) and len(names) <= 1


def test_embedded_image_data_is_not_searched_for_words():
    s = pp.Scanner(["Quokka"], [])
    noise = "abc" + "Quokka" + "Q" * 40                    # a word that happens to appear in base64
    assert not s.scan_text('<img src="data:image/png;base64,' + noise + '">', "a.html", "tree")
    assert s.scan_text("plain Quokka text", "a.md", "tree")


def test_home_path_is_a_directory_not_an_app_route():
    assert any(f.rule == "home-path" for f in _scan("data in " + "/home" + "/someone/x"))
    assert not any(f.rule == "home-path" for f in _scan('hx-post="/review/home/delete"'))
