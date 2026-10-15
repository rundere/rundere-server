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
  infra        home-network detail: .home.example / DDNS hosts, private IPv4
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
import fnmatch
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import tomllib
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
    r"not[_-]?a[_-]?real|fake|test[_-]?only|ui[_-]?check|^(.)\1+$")

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

        def add(check, rule, sev, lineno, value, detail=""):
            out.append(Finding(check, rule, sev, path, lineno, detail or mask(value), fingerprint(value), where))

        for lineno, line in enumerate(text.splitlines(), 1):
            if len(line) > 20_000:          # minified or embedded data: scan, but don't drown
                line = line[:20_000]
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
                    add("secrets", "assigned-secret", BLOCK, lineno, value,
                        f"{m.group('name')} = {mask(value)}")
            for s, rx in self.deny_strings:
                for m in rx.finditer(line):
                    add("denylist", "deny-string", BLOCK, lineno, m.group(0))
            for rx in self.deny_regexes:
                for m in rx.finditer(line):
                    add("denylist", "deny-regex", BLOCK, lineno, m.group(0))
            for rule, rx in INFRA_RULES:
                for m in rx.finditer(line):
                    add("infra", rule, WARN, lineno, m.group(0), m.group(0) if rule != "home-path" else mask(m.group(0)))
            for m in PERSONAL_EMAIL.finditer(line):
                add("email", "personal-email", WARN, lineno, m.group(0))
        return out

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
            if e and not e.endswith("noreply.github.com") and e != "noreply@anthropic.com":
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
    args = ap.parse_args(argv)
    try:
        allow = load_allowlist()
        deny_strings, deny_regexes = load_denylist()
    except ConfigError as e:
        print(f"prepublish: {e}", file=sys.stderr)
        return 2
    scanner = Scanner(deny_strings, deny_regexes)
    findings = check_tree(scanner) + check_decisions()
    if args.history:
        findings += check_history(scanner)
    if not args.no_tools:
        findings += check_bandit() + check_deps()
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
