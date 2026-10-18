# Publishing safely: the pre-publish check

Run·dere is meant to be published as source code. Anything that reaches a public
repository stays public, including in its history. So publishing never depends on
someone remembering a checklist: one check runs the same way locally, in CI and
before a release (YONDER-287, part of the public-launch epic YONDER-59).

## Run it

```bash
uv run --extra dev python scripts/prepublish.py              # the files in this checkout
uv run --extra dev python scripts/prepublish.py --history    # every version ever committed
uv run --extra dev python scripts/prepublish.py --no-tools --out report.md --json report.json
```

Exit status: `0` no blocking findings · `1` blocking findings · `2` a config problem.

The report never prints a matched value whole. It shows a short masked form and
a fingerprint (the first 12 hex characters of its SHA-256), so you can share it.

## What it checks

| Check | Looks for | Severity |
|---|---|---|
| secrets | private keys; GitHub, AWS, Slack, Google, Stripe, Anthropic and OpenAI token formats; JWTs; passwords in URLs; long random-looking values assigned to secret-ish names | blocking |
| denylist | **your** private strings: names, emails, hostnames, IP ranges, paths (see below) | blocking |
| files | dumps, databases, keys, `.env`, `*.local.*` configs, `.claude/`; files over 1 MB | blocking |
| infra | `.home.example` / `.lan` / DDNS hostnames, private IPv4 literals, home-directory paths; **public** IPv4 addresses (well-known public DNS, example.com and Anthropic's published range are named, not flagged; spec section numbers and lockfiles are skipped) | warning |
| email | personal webmail addresses | warning |
| personal | booking references, ticket numbers and phone numbers | warning |
| decisions | a cited D-number that no committed spec defines | warning |
| docs | relative Markdown links to files that don't exist | warning |
| naming | the old name "Yonder" (one summary line with counts) | warning |
| licences | runtime dependencies whose licence can't be combined with AGPL-3.0, or that declare none (with tools) | warning |
| bandit | static analysis of the Python code; high severity with medium or high confidence blocks | blocking / warning |
| deps | known vulnerabilities in installed dependencies (pip-audit) | blocking |
| history (`--history`) | all of the above in every past version of every file, plus files that were ever committed, commit messages, and personal author emails | as above |

Placeholders (`CHANGE_ME`, `example`, `<…>`, `${…}`, `your-…`) are not secrets and
aren't reported.

Secrets, the denylist and personal emails are also checked in **decoded** form:
base64 runs and URL-encoding are decoded and scanned again, so a name or token
hidden in encoded text is still found. The report marks those "(in base64 text)".

## Your private denylist

The things you most need to keep out (your name, your email, your home hostnames
and network) can't be listed in the repository, or the list would publish them.
So the denylist lives outside it:

- **Locally:** copy `docs/prepublish.local.example.toml` to `.prepublish.local.toml`
  in the repo root. It's git-ignored, and the check refuses to let it be committed.
- **In CI:** put the same TOML in the repository secret `PREPUBLISH_DENYLIST`.

Without a denylist the report says so at the top. The generic checks still run,
but your own details aren't checked.

## Rewriting history from the denylist

If history is ever rewritten before publishing (option (c), YONDER-286), the
same denylist drives the rewrite, so the rewrite and the check that verifies it
can't drift apart. Add to `.prepublish.local.toml`:

```toml
[rewrite]                 # replacement for each denied string or regex (default: "redacted")
"you@example.com" = "owner@example.com"

[identity]                # every personal commit identity becomes this
noreply = "12345+you@users.noreply.github.com"

[drop]                    # files removed from all of history
paths = ["NOTES.md", "private/"]
```

Then:

```bash
uv run python scripts/prepublish.py --rewrite-rules /some/folder/outside/the/repo
```

writes `replace-text.txt`, `replace-message.txt`, `mailmap`, `drop-paths.txt` and a
README with the `git-filter-repo` command. They contain your private strings, so
the check refuses to write them inside the repository. A name keeps its case
(`NAME`→`ALEX`, `Name`→`Alex`), and "Merge pull request #N" becomes plain text.
Run the rewrite on a **mirror clone**, verify it with `--history`, and push the
result only to a **new** repository: GitHub keeps an old repository's PRs and
their diffs.

## Accepting a finding

Some findings are fine: the SSRF policy has to list private IP ranges, and a test
fixture may need a fake token. Accept each one in `.prepublish-allow.toml`, as
narrowly as you can, **with a reason**:

```toml
[[allow]]
rule = "private-ipv4"
path = "app/net_policy.py"
reason = "The SSRF policy lists the private ranges it refuses."
```

Fields: `check`, `rule`, `path` (a glob), `fingerprint` (from the report, for one
specific value), `where` (`tree`, `history` or `commit`), `reason` (required).
An entry without a reason is a config error.

## Where it runs

- **Every push and PR:** the `prepublish` job in `.github/workflows/ci.yml`, on the
  current files. It's report-only (`continue-on-error`) until the public-launch steps
  clear the baseline, then it becomes required.
- **Before publishing:** run with `--history` and review the report in full.
- **Releases:** once the baseline is clean, "cut release" runs it as a gate.

## Manual checks, every time the repository is published

The script can't judge these:

- [ ] **Screenshots and images** (`docs/`, `design/`) show only demo data (D-198).
- [ ] **New docs** read correctly for a stranger: no homelab specifics, no Plane links.
- [ ] **New settings** are documented with safe defaults.
- [ ] **New dependencies** have a reason in their PR, and their licences are compatible
      with AGPL-3.0.
- [ ] **New endpoints** have an authentication and scoping review (`app/scope.py`).
- [ ] **The report's warnings** were read, not just the blocking count.
