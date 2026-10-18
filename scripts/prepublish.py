#!/usr/bin/env python3
"""Run·dere's pre-publish check (YONDER-287, part of the public-launch epic YONDER-59).

One check, run the same way locally, in CI and before a release, so publishing
never depends on someone remembering a checklist. It looks for what must never
reach a public repository, and for code-level risk:

  secrets      token formats, private keys, credentials in URLs, and long
               random-looking values assigned to secret-ish names
  denylist     YOUR private strings (names, emails, hostnames, IP ranges, paths),
               read from a file that is never committed (.prepublish.local.toml),
               or from the PREPUBLISH_DENYLIST environment variable in CI
  infra        home-network detail: .lan / .internal-style and DDNS hosts, private IPv4
               literals, home-directory paths (warnings: some are legitimate)
  email        personal webmail addresses (warnings)
  files        files that shouldn't be in a repo at all (dumps, databases, keys,
               .env, local configs) and oversized files
  decisions    every D-number cited in the repo is defined in a committed spec
  bandit       static analysis of the Python code (when bandit is installed)
  deps         known vulnerabilities in installed dependencies (pip-audit)

Two scopes:
  (default)    the files tracked in the current checkout
  --history    every version of every file ever committed (each scanned once),
               plus commit authors and messages: what a public history exposes

Matched values are never printed whole: the report shows a short masked form
and a fingerprint, so the report itself is safe to share. Accepted findings go
in .prepublish-allow.toml, each with a written reason.

    uv run python scripts/prepublish.py                 # current files
    uv run python scripts/prepublish.py --history       # the whole history
    uv run python scripts/prepublish.py --json out.json --no-tools

Exit status: 0 = no blocking findings, 1 = blocking findings, 2 = bad config.
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import threading
import tomllib
import urllib.parse
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOW_FILE = ROOT / ".prepublish-allow.toml"
DENY_FILE = ROOT / ".prepublish.local.toml"
MAX_BYTES = 1_000_000          # files bigger than this need an allow entry
MAX_SCAN_BYTES = 5_000_000     # don't content-scan anything larger

BLOCK, WARN = "block", "warn"


# --------------------------------------------------------------------------- findings
@dataclass
class Finding:
    check: str
    rule: str
    severity: str
    path: str
    line: int = 0
    detail: str = ""
    fingerprint: str = ""      # sha256 of the matched value, first 12 hex chars
    where: str = "tree"        # tree | history | commit
    allowed_by: str = ""

    def key(self) -> tuple:
        return (self.check, self.rule, self.path, self.line, self.fingerprint, self.where)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def mask(value: str) -> str:
    """Enough to recognise a finding, never enough to reuse it."""
    v = value.strip()
    if len(v) <= 4:
        return "*" * len(v)
    return f"{v[:2]}…{v[-1:]} ({len(v)} chars)"


# --------------------------------------------------------------------------- rules
# Token formats with near-zero false positives: always blocking.
SECRET_RULES: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe-live-key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("credentials-in-url", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@'\"]{1,64}:[^/\s@'\"]{6,}@[A-Za-z0-9.-]+")),
]

# name = "value" / name: value / NAME=value, for secret-ish names. Only flagged
# when the value looks random (entropy) and isn't an obvious placeholder.
ASSIGNED_SECRET = re.compile(
    r"""(?ix)
    (?P<name>[a-z0-9_.-]*(?:secret|password|passwd|pwd|token|api[_-]?key|private[_-]?key|
                            client[_-]?secret|auth[_-]?key|access[_-]?key|encryption[_-]?key)[a-z0-9_.-]*)
    ["']?\s*[:=]\s*(?P<quote>["']?)
    (?P<value>[A-Za-z0-9+/_=.~-]{20,})
    """)
# An unquoted value that is a name or attribute chain is code, not a secret:
# `password = settings.demo_password`, `token = auth.issue_web_token(user)`.
CODE_EXPR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
ALPHABET = re.compile(r"abcdefgh|ABCDEFGH|0123456789")
PLACEHOLDER = re.compile(
    r"(?i)change[_-]?me|example|placeholder|dummy|sample|your[_-]|xxxx|<[^>]+>|\$\{|\{\{|redacted|"
    r"not[_-]?a[_-]?real|fake|test[_-]?only|ui[_-]?check|^(.)\1+$|"
    # Labelled throwaways, as good as CHANGE_ME (owner, 2026-10-17): devonly, dev-only, not-for-prod.
    r"dev[_-]?only|not[_-]?for[_-]?prod")

# Home-network and personal detail that is often fine in examples: warnings.
INFRA_RULES: list[tuple[str, re.Pattern]] = [
    ("home-local-host", re.compile(r"\b[a-z0-9][a-z0-9-]*\.(?:home\.local|lan|internal|home\.arpa)\b", re.I)),
    ("ddns-host", re.compile(r"\b[a-z0-9][a-z0-9-]*\.(?:ddns\.net|no-ip\.[a-z]+|duckdns\.org|dynu\.net|hopto\.org)\b", re.I)),
    ("private-ipv4", re.compile(
        r"(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d.])")),
    ("home-path", re.compile(r"(?:/home|/Users|/media)/[a-z][a-z0-9_.-]+")),
]
PERSONAL_EMAIL = re.compile(
    r"\b[A-Za-z0-9._%+-]+@(?:gmail|googlemail|proton|protonmail|pm|outlook|hotmail|live|yahoo|icloud|me|aol)"
    r"\.(?:com|me|ch|ca|co\.uk)\b", re.I)

# Booking details that are personal when real (warnings: fixtures use made-up ones).
PERSONAL_RULES: list[tuple[str, re.Pattern]] = [
    ("booking-reference", re.compile(
        r"(?i)\b(?:pnr|booking(?:\s+(?:ref(?:erence)?|code|no\.?|number))?|confirmation(?:\s+(?:code|no\.?|number|#))?|"
        r"record\s+locator|reservation(?:\s+(?:code|no\.?|number))?)\b[\s:#=\"'-]{1,6}"
        r"(?P<v>(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5,8})\b")),
    ("ticket-number", re.compile(r"(?i)\b(?:e-?ticket|ticket)(?:\s*(?:no\.?|number|#))?[^0-9\n]{0,12}(?P<v>\d{3}[- ]?\d{10})\b")),
    ("phone-number", re.compile(r"(?<![\w+])(?P<v>\+\d{1,3}[ .-]?(?:\(\d{1,4}\)[ .-]?)?\d{2,4}[ .-]?\d{3,4}[ .-]?\d{3,4})(?!\d)")),
]

# Public IPv4 addresses: a home server's public address is the one thing a
# private subnet can't stand in for. Well-known public services are named, not
# flagged; version strings and spec section numbers (§3.1.3.5) are skipped.
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.])")
KNOWN_PUBLIC = [
    ipaddress.ip_network(n) for n in (
        "1.1.1.1/32", "1.0.0.1/32", "9.9.9.9/32", "149.112.112.112/32", "8.8.8.8/32", "8.8.4.4/32",
        "208.67.222.222/32", "208.67.220.220/32",        # public DNS resolvers
        "93.184.216.34/32",                              # example.com
        "160.79.104.0/21",                               # Anthropic's published outbound range
        "75.2.60.5/32",                                  # Netlify's load balancer (rundere.app DNS)
    )]
SPEC_CONTEXT = re.compile(r"(?i)§|\bsection\b|\brfc\b|\bversion\b|==|>=|<=")
LOCKFILES = ("uv.lock", "poetry.lock", "package-lock.json", "yarn.lock", "Pipfile.lock")

# Embedded files (an image in HTML): their base64 is noise to a word search and
# yields false hits; the decoded view below still checks any text inside them.
DATA_URI = re.compile(r"data:[\w.+/-]+;base64,[A-Za-z0-9+/=]{16,}")

# Encoded copies: a secret or a name hidden in base64 or URL-encoding is still there.
B64_RUN = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")
PCT = re.compile(r"%[0-9A-Fa-f]{2}")

# Files that have no business in a repository.
FORBIDDEN_NAMES = [
    "*.sql", "*.sql.gz", "*.sqlite", "*.sqlite3", "*.db", "*.dump", "*.bak", "*.backup",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "id_rsa*", "id_ed25519*",
    ".env", ".env.*", "*.local.json", "*.local.toml", "*.local.yml", "*.local.yaml",
    ".netrc", ".pgpass", "*.kdbx", "*.ovpn", "*.mobileprovision", "*.eml", "*.mbox",
    ".claude/*", ".prepublish.local.toml",
]
FORBIDDEN_EXCEPT = [".env.example", "*.example.json", "*.example.toml", "*.example.yml"]

D_CITE = re.compile(r"\bD-(\d{1,3})\b")
D_DEFINE = re.compile(r"\*\*D-(\d{1,3})\b")                      # **D-123 — …
D_RANGE = re.compile(r"\bD-(\d{1,3})\s*[–-]\s*D-(\d{1,3})\b")     # D-110–D-116 (section headings)


def entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


# --------------------------------------------------------------------------- config
class ConfigError(Exception):
    pass


def load_allowlist(path: Path = ALLOW_FILE) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name}: {e}") from e
    entries = data.get("allow", [])
    for i, e in enumerate(entries):
        if not str(e.get("reason", "")).strip():
            raise ConfigError(f"{path.name}: entry {i + 1} has no reason; every exception needs one")
        if not (e.get("rule") or e.get("check")):
            raise ConfigError(f"{path.name}: entry {i + 1} names no rule or check")
    return entries


def denylist_text(path: Path = DENY_FILE) -> str:
    text = os.environ.get("PREPUBLISH_DENYLIST", "")
    if not text and path.exists():
        text = path.read_text()
    return text


def load_denylist(path: Path = DENY_FILE) -> tuple[list[str], list[re.Pattern]]:
    """Your private strings. Never committed: a local file, or PREPUBLISH_DENYLIST
    (the same TOML) in CI. Returns ([], []) when neither is set."""
    text = os.environ.get("PREPUBLISH_DENYLIST", "")
    if not text and path.exists():
        text = path.read_text()
    if not text.strip():
        return [], []
    try:
        data = tomllib.loads(text).get("deny", {})
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"denylist: {e}") from e
    strings = [s for s in data.get("strings", []) if str(s).strip()]
    try:
        regexes = [re.compile(r, re.I) for r in data.get("regexes", [])]
    except re.error as e:
        raise ConfigError(f"denylist regex: {e}") from e
    return strings, regexes


def is_allowed(f: Finding, allow: list[dict]) -> str:
    for e in allow:
        if e.get("check") and e["check"] != f.check:
            continue
        if e.get("rule") and e["rule"] != f.rule:
            continue
        if e.get("path") and not fnmatch.fnmatch(f.path, e["path"]):
            continue
        if e.get("fingerprint") and e["fingerprint"] != f.fingerprint:
            continue
        if e.get("where") and e["where"] != f.where:
            continue
        return e["reason"]
    return ""


# --------------------------------------------------------------------------- content scan
class Scanner:
    def __init__(self, deny_strings: list[str], deny_regexes: list[re.Pattern]):
        self.deny_strings = [(s, re.compile(re.escape(s), re.I)) for s in deny_strings]
        self.deny_regexes = deny_regexes

    def scan_text(self, text: str, path: str, where: str) -> list[Finding]:
        out: list[Finding] = []
        is_lockfile = path.rsplit("/", 1)[-1] in LOCKFILES

        def add(check, rule, sev, lineno, value, detail="", via=""):
            d = detail or mask(value)
            out.append(Finding(check, rule, sev, path, lineno, f"{d} (in {via} text)" if via else d,
                               fingerprint(value), where))

        for lineno, line in enumerate(text.splitlines(), 1):
            if len(line) > 20_000:          # minified or embedded data: scan, but don't drown
                line = line[:20_000]
            plain = DATA_URI.sub("data:…", line) if "base64," in line else line
            self._sensitive(plain, lineno, add)
            for rule, rx in INFRA_RULES:
                for m in rx.finditer(plain):
                    add("infra", rule, WARN, lineno, m.group(0), m.group(0) if rule != "home-path" else mask(m.group(0)))
            if not is_lockfile and not SPEC_CONTEXT.search(plain):
                for m in IPV4.finditer(plain):
                    try:
                        ip = ipaddress.ip_address(m.group(1))
                    except ValueError:
                        continue
                    if ip.is_global and not any(ip in n for n in KNOWN_PUBLIC):
                        add("infra", "public-ipv4", WARN, lineno, m.group(1), m.group(1))
            for rule, rx in PERSONAL_RULES:
                for m in rx.finditer(plain):
                    add("personal", rule, WARN, lineno, m.group("v"))
            # The same line, decoded: base64 runs and URL-encoding.
            for via, decoded in self._decoded(line):
                self._sensitive(decoded, lineno, lambda *a, **k: add(*a, via=via, **k))
        return out

    def _sensitive(self, line: str, lineno: int, add) -> None:
        """The checks that matter even when hidden in encoded text."""
        for rule, rx in SECRET_RULES:
            for m in rx.finditer(line):
                if rule == "credentials-in-url" and PLACEHOLDER.search(m.group(0).split("@")[0]):
                    continue            # postgres://user:CHANGE_ME@db — a template, not a secret
                add("secrets", rule, BLOCK, lineno, m.group(0))
        for m in ASSIGNED_SECRET.finditer(line):
            value = m.group("value")
            if PLACEHOLDER.search(value) or PLACEHOLDER.search(line) or ALPHABET.search(value):
                continue
            if not m.group("quote") and (CODE_EXPR.match(value) or line[m.end("value"):m.end("value") + 1] == "("):
                continue
            if entropy(value) >= 3.5 and not value.isdigit():
                add("secrets", "assigned-secret", BLOCK, lineno, value, f"{m.group('name')} = {mask(value)}")
        for s, rx in self.deny_strings:
            for m in rx.finditer(line):
                add("denylist", "deny-string", BLOCK, lineno, m.group(0))
        for rx in self.deny_regexes:
            for m in rx.finditer(line):
                add("denylist", "deny-regex", BLOCK, lineno, m.group(0))
        for m in PERSONAL_EMAIL.finditer(line):
            add("email", "personal-email", WARN, lineno, m.group(0))

    @staticmethod
    def _decoded(line: str) -> list[tuple[str, str]]:
        views = []
        if PCT.search(line):
            u = urllib.parse.unquote(line)
            if u != line:
                views.append(("url-encoded", u))
        for m in B64_RUN.finditer(line):
            run = m.group(0)
            try:
                raw = base64.b64decode(run + "=" * (-len(run) % 4), validate=False)
                txt = raw.decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue                    # binary (an embedded image) or not base64 at all
            if len(txt) >= 12 and sum(c.isprintable() or c in "\n\t" for c in txt) / len(txt) > 0.95:
                views.append(("base64", txt))
        return views

    def scan_name(self, path: str, where: str) -> list[Finding]:
        """The path itself can leak (a username in a directory name)."""
        out = []
        for s, rx in self.deny_strings:
            if rx.search(path):
                out.append(Finding("denylist", "deny-in-path", BLOCK, path, 0, "path contains a denied string",
                                   fingerprint(s), where))
        return out


def forbidden_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatch(name, p) for p in FORBIDDEN_EXCEPT):
        return False
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(path, p) for p in FORBIDDEN_NAMES)


# --------------------------------------------------------------------------- git helpers
def git(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, input=data, capture_output=True, check=True).stdout


def tracked_files() -> list[str]:
    return [p for p in git("ls-files", "-z").decode().split("\0") if p]


def read_blob_text(raw: bytes) -> str | None:
    if b"\0" in raw[:8000]:
        return None                       # binary
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------- checks
def check_tree(scanner: Scanner) -> list[Finding]:
    out: list[Finding] = []
    for path in tracked_files():
        p = ROOT / path
        if not p.is_file():
            continue
        size = p.stat().st_size
        if forbidden_file(path):
            out.append(Finding("files", "forbidden-file", BLOCK, path, 0, "this kind of file doesn't belong in a repo"))
        if size > MAX_BYTES:
            out.append(Finding("files", "large-file", BLOCK, path, 0, f"{size / 1e6:.1f} MB (limit {MAX_BYTES / 1e6:.0f} MB)"))
        out += scanner.scan_name(path, "tree")
        if size > MAX_SCAN_BYTES:
            continue
        text = read_blob_text(p.read_bytes())
        if text is not None:
            out += scanner.scan_text(text, path, "tree")
    return out


def check_history(scanner: Scanner) -> list[Finding]:
    """Every blob ever committed, once, plus commit metadata. A finding's path is
    the first path the blob was seen under."""
    out: list[Finding] = []
    seen_paths: dict[str, str] = {}
    for line in git("rev-list", "--objects", "--all").decode("utf-8", "replace").splitlines():
        sha, _, path = line.partition(" ")
        if path and sha not in seen_paths:
            seen_paths[sha] = path
    ever_paths = set(seen_paths.values())
    for path in sorted(ever_paths):
        if forbidden_file(path):
            out.append(Finding("files", "forbidden-file", BLOCK, path, 0,
                               "was committed at some point (still in history)", where="history"))
        out += scanner.scan_name(path, "history")
    # Only blobs: ask for type and size first, then the content of text-sized blobs.
    shas = list(seen_paths)
    meta = git("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
               data="\n".join(shas).encode()).decode().splitlines()
    blobs = [(s, int(n)) for s, t, n in (m.split() for m in meta) if t == "blob"]
    for sha, size in blobs:
        if size > MAX_BYTES:
            out.append(Finding("files", "large-file", WARN, seen_paths[sha], 0,
                               f"{size / 1e6:.1f} MB blob in history", where="history"))
    wanted = [s for s, n in blobs if n <= MAX_SCAN_BYTES]
    proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=ROOT, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    assert proc.stdin and proc.stdout

    def feed():
        # Written from a thread: writing every id before reading would deadlock
        # once git's output pipe fills.
        try:
            proc.stdin.write(("\n".join(wanted) + "\n").encode())
        finally:
            proc.stdin.close()

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    for _ in wanted:
        header = proc.stdout.readline().split()
        sha, size = header[0].decode(), int(header[2])
        raw = proc.stdout.read(size)
        proc.stdout.read(1)               # trailing newline
        text = read_blob_text(raw)
        if text is not None:
            out += scanner.scan_text(text, seen_paths[sha], "history")
    writer.join()
    proc.wait()
    # Commit metadata: who, and what the messages say.
    log = git("log", "--all", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x01").decode("utf-8", "replace")
    emails: Counter = Counter()
    for rec in log.split("\x01"):
        parts = rec.strip("\n").split("\x00")
        if len(parts) < 6:
            continue
        h, an, ae, cn, ce, body = parts
        for e in (ae, ce):
            if e and not e.endswith("noreply.github.com") and e not in ("noreply@anthropic.com",
                                                                         "noreply@github.com"):
                emails[e] += 1
        for f in scanner.scan_text(body, f"commit {h[:10]}", "commit"):
            out.append(f)
    for e, n in emails.items():
        out.append(Finding("history", "author-email", WARN, "commit metadata", 0,
                           f"{mask(e)} on {n} commits (use a noreply address for public history)",
                           fingerprint(e), "commit"))
    return out


def check_decisions() -> list[Finding]:
    """Every D-number cited anywhere tracked must be defined in a committed .md."""
    defined: set[int] = set()
    cites: dict[int, str] = {}
    for path in tracked_files():
        p = ROOT / path
        if not p.is_file() or p.stat().st_size > MAX_SCAN_BYTES:
            continue
        text = read_blob_text(p.read_bytes())
        if text is None:
            continue
        if path.endswith(".md"):
            defined.update(int(n) for n in D_DEFINE.findall(text))
            # A range in a heading ("## D-110–D-116 — …") defines that block of decisions.
            for a, b in D_RANGE.findall(text):
                if re.search(rf"^#+.*D-{a}\s*[–-]\s*D-{b}", text, re.M):
                    defined.update(range(int(a), int(b) + 1))
        for m in D_CITE.finditer(text):
            cites.setdefault(int(m.group(1)), path)
    out = []
    for n in sorted(set(cites) - defined):
        out.append(Finding("decisions", "undefined-decision", WARN, cites[n], 0,
                           f"D-{n} is cited but not defined in any committed spec"))
    return out


def run_tool(cmd: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return True, r.stdout


def check_bandit() -> list[Finding]:
    ok, out = run_tool([sys.executable, "-m", "bandit", "-q", "-r", "app", "extract", "resolve", "adapters",
                        "discover", "planning", "rules.py", "validation.py", "-f", "json"])
    if not ok or not out.strip().startswith("{"):
        return [Finding("bandit", "not-run", WARN, "-", 0, "bandit isn't installed (uv run --extra dev)")]
    findings = []
    for r in json.loads(out).get("results", []):
        sev = BLOCK if (r["issue_severity"] == "HIGH" and r["issue_confidence"] in ("HIGH", "MEDIUM")) else WARN
        findings.append(Finding("bandit", r["test_id"], sev, r["filename"].removeprefix(str(ROOT) + "/"),
                                r["line_number"], f"{r['issue_severity'].lower()}: {r['issue_text']}",
                                fingerprint(f"{r['test_id']}:{r['code'].strip()}")))
    return findings


def check_deps() -> list[Finding]:
    ok, out = run_tool([sys.executable, "-m", "pip_audit", "--format", "json", "--progress-spinner", "off"])
    if not ok or not out.strip().startswith(("{", "[")):
        return [Finding("deps", "not-run", WARN, "-", 0, "pip-audit isn't installed or couldn't reach the advisory database")]
    data = json.loads(out)
    deps = data.get("dependencies", data) if isinstance(data, dict) else data
    findings = []
    for d in deps:
        for v in d.get("vulns", []):
            fix = ", ".join(v.get("fix_versions", [])) or "no fix yet"
            findings.append(Finding("deps", "vulnerable-dependency", BLOCK, f"{d['name']}=={d['version']}", 0,
                                    f"{v['id']} (fixed in: {fix})", fingerprint(f"{d['name']}:{v['id']}")))
    return findings


# Licences that can be combined with the AGPL-3.0 server. Anything else, or a
# package that doesn't declare one, is a warning to look at.
AGPL_COMPATIBLE = re.compile(
    r"(?i)\b(mit|bsd|apache|isc|psf|python software foundation|mpl|mozilla|lgpl|lesser general|"
    r"gplv3|gpl-3|gnu general public license v3|agpl|affero|unlicense|cc0|zlib|hpnd|0bsd|"
    r"public domain|historical permission)")
GPL2_ONLY = re.compile(r"(?i)gpl-?2\.0-only|gplv2(?!\+)|gnu general public license v2 \(gplv2\)")


def _runtime_distributions() -> dict[str, object]:
    """The project's runtime dependencies, transitively (not the dev extra)."""
    from importlib import metadata
    try:
        from packaging.requirements import Requirement
    except ImportError:                    # packaging ships with pip; fall back to names only
        Requirement = None
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    todo = list(pyproject.get("project", {}).get("dependencies", []))
    seen: dict[str, object] = {}
    while todo:
        req = todo.pop()
        if Requirement:
            r = Requirement(req)
            if r.marker and not r.marker.evaluate({"extra": ""}):
                continue
            name = r.name
        else:
            name = re.split(r"[<>=!~;\[ ]", req, maxsplit=1)[0]
        key = name.lower().replace("_", "-")
        if key in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        seen[key] = dist
        todo.extend(dist.requires or [])
    return seen


def check_licences() -> list[Finding]:
    out = []
    for key, dist in sorted(_runtime_distributions().items()):
        meta = dist.metadata
        text = " ".join(filter(None, [meta.get("License-Expression", ""), meta.get("License", "")[:200],
                                      *[c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]]))
        label = f"{meta['Name']}=={dist.version}"
        if GPL2_ONLY.search(text) and not AGPL_COMPATIBLE.search(text.replace("GPLv2", "")):
            out.append(Finding("licences", "gpl2-only", WARN, label, 0, "GPL-2.0-only can't be combined with AGPL-3.0"))
        elif not text.strip():
            out.append(Finding("licences", "unknown-licence", WARN, label, 0, "no licence declared in its metadata"))
        elif not AGPL_COMPATIBLE.search(text):
            out.append(Finding("licences", "check-licence", WARN, label, 0, f"licence to check: {text[:80]}"))
    return out


MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def check_doc_links() -> list[Finding]:
    """Relative links in Markdown that point at nothing."""
    out = []
    for path in tracked_files():
        if not path.endswith(".md"):
            continue
        p = ROOT / path
        if not p.is_file():
            continue
        for lineno, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            for m in MD_LINK.finditer(line):
                target = m.group(1)
                if re.match(r"(?i)^(?:[a-z][a-z0-9+.-]*:|#|//)", target):
                    continue                # URLs, mailto:, anchors
                local = target.split("#", 1)[0].split("?", 1)[0]
                local = re.sub(r":\d+(?:-\d+)?$", "", local)       # file.py:42
                if local and not (p.parent / urllib.parse.unquote(local)).exists():
                    out.append(Finding("docs", "broken-link", WARN, path, lineno, f"no such file: {local}"))
    return out


def check_naming() -> list[Finding]:
    """The old name, Yonder (renamed in 4.4.0): one summary, not one per line."""
    counts: Counter = Counter()
    for path in tracked_files():
        p = ROOT / path
        if not p.is_file() or p.stat().st_size > MAX_SCAN_BYTES:
            continue
        text = read_blob_text(p.read_bytes())
        if text:
            n = len(re.findall(r"\bYonder\b", text))
            if n:
                counts[path] = n
    if not counts:
        return []
    top = ", ".join(f"{p} ({n})" for p, n in counts.most_common(5))
    return [Finding("naming", "old-name", WARN, "(repository)", 0,
                    f"'Yonder' appears {sum(counts.values())} times in {len(counts)} files; most in {top}")]


# --------------------------------------------------------------------------- rewrite rules
def write_rewrite_rules(out_dir: Path, deny_text: str) -> list[str]:
    """Turn the private denylist into git-filter-repo inputs, so a history rewrite
    and the check that verifies it share one source of truth. Never writes into
    the repository: out_dir must be outside it."""
    data = tomllib.loads(deny_text) if deny_text.strip() else {}
    deny, rewrite = data.get("deny", {}), data.get("rewrite", {})
    ident, drop = data.get("identity", {}), data.get("drop", {})
    if ROOT in out_dir.resolve().parents or out_dir.resolve() == ROOT:
        raise ConfigError("--rewrite-rules must point outside the repository")
    out_dir.mkdir(parents=True, exist_ok=True)

    def rules_for(match: str, repl: str, is_regex: bool) -> list[str]:
        src = match if is_regex else re.escape(match)
        lines = []
        if not is_regex and repl.isalpha() and match.isalpha():
            # Keep the case of each occurrence: NAME→REPL, Name→Repl, name→repl.
            for m, r in ((match.upper(), repl.upper()), (match.title(), repl.title()), (match.lower(), repl.lower())):
                lines.append(f"literal:{m}==>{r}")
        lines.append(f"regex:(?i){src}==>{repl}")
        return lines

    text_rules: list[str] = []
    for sstr in deny.get("strings", []):
        text_rules += rules_for(sstr, rewrite.get(sstr, "redacted"), False)
    for rx in deny.get("regexes", []):
        text_rules += rules_for(rx, rewrite.get(rx, "redacted"), True)
    (out_dir / "replace-text.txt").write_text("\n".join(text_rules) + "\n")
    msg_rules = text_rules + [r"regex:Merge pull request #(\d+) from==>Merge PR \1 (private archive) from"]
    (out_dir / "replace-message.txt").write_text("\n".join(msg_rules) + "\n")
    (out_dir / "drop-paths.txt").write_text("\n".join(drop.get("paths", [])) + "\n")
    written = ["replace-text.txt", "replace-message.txt", "drop-paths.txt"]
    if ident.get("noreply"):
        keep = {"noreply@anthropic.com", "noreply@github.com"}
        name = ident.get("name") or ident["noreply"].split("+", 1)[-1].split("@", 1)[0]
        seen, lines = set(), []
        for fmt in ("%an%x00%ae", "%cn%x00%ce"):
            for rec in git("log", "--all", f"--format={fmt}").decode("utf-8", "replace").splitlines():
                n, _, e = rec.partition("\0")
                if not e or e in keep or e.endswith("users.noreply.github.com") or "[bot]" in n or (n, e) in seen:
                    continue
                seen.add((n, e))
                lines.append(f"{name} <{ident['noreply']}> {n} <{e}>")
        (out_dir / "mailmap").write_text("\n".join(lines) + "\n")
        written.append("mailmap")
    (out_dir / "README.txt").write_text(
        "Generated by scripts/prepublish.py --rewrite-rules from your PRIVATE denylist.\n"
        "Contains your private strings: keep it out of every repository.\n\n"
        "Run on a fresh MIRROR clone, never on your working repository:\n"
        "  git clone --mirror <repo> rewrite.git && cd rewrite.git\n"
        "  uv tool run --from git-filter-repo git-filter-repo \\\n"
        "    --invert-paths --paths-from-file drop-paths.txt \\\n"
        "    --replace-text replace-text.txt --replace-message replace-message.txt \\\n"
        "    --mailmap mailmap --force\n"
        "Then verify: clone it and run prepublish.py --history with PREPUBLISH_DENYLIST set.\n"
        "Push the result only to a NEW repository (GitHub keeps PRs of the old one).\n")
    return written


# --------------------------------------------------------------------------- report
def report(findings: list[Finding], history: bool, denylist_on: bool) -> str:
    lines = [f"# Pre-publish check ({'full history' if history else 'current files'})", ""]
    if not denylist_on:
        lines += ["> No private denylist loaded (.prepublish.local.toml or PREPUBLISH_DENYLIST): "
                  "your own names, hosts and paths were not checked.", ""]
    active = [f for f in findings if not f.allowed_by]
    blocking = [f for f in active if f.severity == BLOCK]
    by_check = Counter((f.check, f.severity) for f in active)
    lines.append(f"**{len(blocking)} blocking**, {len(active) - len(blocking)} warnings, "
                 f"{len(findings) - len(active)} accepted (allowlist).")
    lines.append("")
    lines.append("| check | blocking | warnings |")
    lines.append("|---|---:|---:|")
    for check in sorted({c for c, _ in by_check}):
        lines.append(f"| {check} | {by_check[(check, BLOCK)]} | {by_check[(check, WARN)]} |")
    for sev, title in ((BLOCK, "Blocking"), (WARN, "Warnings")):
        group = sorted((f for f in active if f.severity == sev), key=lambda f: (f.check, f.rule, f.path, f.line))
        if not group:
            continue
        lines += ["", f"## {title}", ""]
        current = None
        for f in group:
            if (f.check, f.rule) != current:
                current = (f.check, f.rule)
                n = sum(1 for g in group if (g.check, g.rule) == current)
                lines.append(f"### {f.check} / {f.rule} ({n})")
            loc = f"{f.path}:{f.line}" if f.line else f.path
            fp = f" `{f.fingerprint}`" if f.fingerprint else ""
            lines.append(f"- {loc}{' [' + f.where + ']' if f.where != 'tree' else ''} — {f.detail}{fp}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--history", action="store_true", help="scan every version ever committed, and commit metadata")
    ap.add_argument("--no-tools", action="store_true", help="skip bandit and pip-audit")
    ap.add_argument("--json", metavar="PATH", help="also write the findings as JSON")
    ap.add_argument("--out", metavar="PATH", help="write the report here instead of stdout")
    ap.add_argument("--rewrite-rules", metavar="DIR",
                    help="write git-filter-repo rules from the private denylist into DIR (outside the repo), then exit")
    args = ap.parse_args(argv)
    if args.rewrite_rules:
        try:
            written = write_rewrite_rules(Path(args.rewrite_rules), denylist_text())
        except (ConfigError, tomllib.TOMLDecodeError) as e:
            print(f"prepublish: {e}", file=sys.stderr)
            return 2
        print(f"wrote {', '.join(written)} to {args.rewrite_rules} (contains your private strings)")
        return 0
    try:
        allow = load_allowlist()
        deny_strings, deny_regexes = load_denylist()
    except ConfigError as e:
        print(f"prepublish: {e}", file=sys.stderr)
        return 2
    scanner = Scanner(deny_strings, deny_regexes)
    findings = check_tree(scanner) + check_decisions() + check_doc_links() + check_naming()
    if args.history:
        findings += check_history(scanner)
    if not args.no_tools:
        findings += check_bandit() + check_deps() + check_licences()
    # Deduplicate (the same blob can be reached from the tree and history).
    unique: dict[tuple, Finding] = {}
    for f in findings:
        unique.setdefault(f.key(), f)
    findings = list(unique.values())
    for f in findings:
        f.allowed_by = is_allowed(f, allow)
    text = report(findings, args.history, bool(deny_strings or deny_regexes))
    if args.out:
        Path(args.out).write_text(text)
    else:
        print(text)
    if args.json:
        Path(args.json).write_text(json.dumps([asdict(f) for f in findings], indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(text)
    return 1 if any(f.severity == BLOCK and not f.allowed_by for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
