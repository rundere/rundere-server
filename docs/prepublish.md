# The pre-publish check

Run·dere is published as source code, and anything that reaches a public repository
stays public, history included. So keeping secrets and personal details out never
depends on someone remembering a checklist: one check, `scripts/prepublish.py`, runs
the same way locally, in CI and before every release.

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
| infra | `.lan` / `.internal` / `.home.arpa` / mDNS-style home hostnames and DDNS hostnames, private IPv4 literals, home-directory paths; **public** IPv4 addresses (well-known public DNS, example.com and Anthropic's published range are named, not flagged; spec section numbers and lockfiles are skipped) | warning |
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

`--rewrite-rules DIR` turns the same denylist into `git-filter-repo` inputs
(`[rewrite]`, `[identity]` and `[drop]` tables in `.prepublish.local.toml`), so a
history rewrite and the check that verifies it can't drift apart. The files hold
your private strings, so they're only written outside the repository. Run a
rewrite on a mirror clone, verify it with `--history`, and push the result only to
a new repository: GitHub keeps an old repository's pull requests and their diffs.

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
  current files. It's a gate: a blocking finding fails the build. A fork's PR runs
  without the private denylist (GitHub gives forks no secrets), and the push to
  `main` runs it again with the denylist.
- **Releases:** the "cut release" workflow runs it before bumping the version, so
  no release is cut from files with a blocking finding.
- **Before publishing:** run with `--history` and review the report in full.
