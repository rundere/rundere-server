# Security policy

Run·dere holds people's travel plans, booking details and, for some, encrypted credentials
for other services. Reports that help keep that safe are very welcome.

## Reporting a vulnerability

**Please report privately, not in a public issue, discussion or pull request.**

Use GitHub's private vulnerability reporting: on this repository, open **Security →
Report a vulnerability**. Only the maintainer sees the report.

Helpful things to include:

- what an attacker could do, and what they need first (an account, a shared trip, network
  access to the server, …);
- steps to reproduce, or a proof of concept against your own test server;
- the version (shown on the **About** page and in `/health`) and anything unusual about
  the setup, such as sign-in turned off or an external SSO provider.

Please test only against a server you run yourself. Don't test against other people's
servers, and don't access or keep other people's data.

## What happens next

Run·dere is maintained by one person, so these are honest targets rather than guarantees:

- an acknowledgement within **7 days**;
- an assessment, and a plan or a request for more detail, within **30 days**;
- a fix released as soon as it's ready, with serious issues first.

The fix ships in a patch release with a GitHub security advisory that describes the issue.
You'll be credited in the advisory unless you'd rather not be.

## Supported versions

Security fixes go into the **latest release line only** (for example, if 4.12.x is
current, the fix ships as the next 4.12.x). Older lines aren't patched, so keep your
server on the latest release. The [releases page](https://github.com/rundere/rundere-server/releases)
lists them.

## In scope

Anything in this repository, for example:

- one person seeing or changing another person's trips, journal, captures or settings
  without being shared on them;
- getting in without signing in, or as someone else;
- making the server fetch internal addresses (server-side request forgery);
- reading the encrypted integration credentials, or the secrets they're encrypted with;
- script injection, or framing that tricks someone into an action.

## Out of scope

- servers whose operator turned sign-in off (`TX_AUTH_REQUIRED=false`), which is meant
  for a trusted single-user network only;
- issues in AirTrail, AdventureLog, Ollama, Photon or other services Run·dere connects
  to, which belong with those projects;
- the iOS app, which lives in its own repository;
- denial of service by sheer volume, and missing hardening with no demonstrated impact.

The public demo server is read-only and holds only invented data. Please don't run
scanners against it.
