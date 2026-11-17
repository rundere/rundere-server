# Self-hosting Run·dere

Run·dere runs as one container plus a Postgres database, behind a reverse proxy
that terminates TLS. Start from
[`deploy/docker-compose.example.yml`](../deploy/docker-compose.example.yml): every
setting it uses is documented inline.

## 1. Before you start

- A host with Docker (Compose or Portainer).
- A reverse proxy with TLS, such as Traefik or Caddy. The example uses Traefik
  labels on an external network called `proxy`.
- A hostname. A public one is needed for the iOS app, AI assistants (MCP) and
  Let's Encrypt. A LAN-only hostname with your own certificate authority also
  works for a home-only setup.

## 2. Secrets

Generate each one, store it somewhere safe, and never commit it:

| Setting | How |
|---|---|
| `TX_JWT_SECRET` | `openssl rand -base64 32` |
| `TX_SECRETS_KEY` | `openssl rand -base64 32`. Back it up **apart from the database**: it encrypts each person's integration tokens (D-151). |
| Database password | Any long random string, used in both `TX_DATABASE_URL` and `POSTGRES_PASSWORD` |

Environment variables show up in Portainer and `docker inspect`. To keep secrets out of
them, point the `*_FILE` form at a Docker secret instead: `TX_JWT_SECRET_FILE`,
`TX_SECRETS_KEY_FILE`, `TX_DATABASE_URL_FILE`, `TX_SMTP_PASSWORD_FILE`,
`TX_INBOX_IMAP_PASSWORD_FILE` and `TX_OIDC_CLIENT_SECRET_FILE`. A file that's set wins over
the plain setting, and an unreadable one stops the server from starting.

## 3. Deploy

1. Copy the example. Replace every `CHANGE_ME` and every example hostname.
2. Point the volumes at **local disk**, never NFS (Postgres corrupts on NFS).
   The app runs as an unprivileged user (uid and gid 10001). On its first start
   it hands its data folders (artifacts, documents, tile cache) to that user.
   The tile cache lives inside the artifacts folder by default
   (`TX_MAP_TILE_CACHE_DIR` is empty), so the one artifacts volume covers it.
   The documents folder is separate and off until you set `TX_DOCUMENTS_DIR`;
   the example stack leaves it unset. Mount a volume for it when you turn file
   capture on.
   Folders on a network share it can't change must already be writable by
   uid 10001. To keep the folders owned by your own account instead, run the
   container as you (`user: "1000:1000"` in the stack, with your uid and gid):
   it then skips the hand-over and still never runs as root, so files an older
   image made as root must be handed over by hand (`sudo chown -R 1000:1000 …`).
   If any data path isn't writable, the server refuses to start and lists them.
3. Deploy the stack. Migrations run at start-up.
   Run it as the image does, with **one server process** (don't add uvicorn's
   `--workers` or put several copies behind a balancer): Discover runs and
   archive builds are tracked by the process running them, and a second
   process would mark them failed.
4. Open the site and create the admin account at `/setup` straight away (see
   "Create the admin first" below).

## 4. Sign-in

- Sign-in is required by default (`TX_AUTH_REQUIRED`, D-203). Keep it on for any
  server others can reach; turning it off is only for a trusted single-user LAN, and
  the server logs a warning while it's off.
- **Create the admin first.** A new server gives the admin account to whoever opens
  `/setup` first. Finish setup before the hostname is reachable by anyone else, for
  example by deploying on the LAN host first or before adding the public route.
- Repeated wrong passwords are refused for 15 minutes, per address and per username
  (`TX_LOGIN_RATE_LIMIT`).
- **Single sign-on (optional):** any OIDC provider. Register
  `https://<your host>/oidc/callback` as the redirect URI. SSO can be forced on
  the public host (`TX_OIDC_FORCE_HOSTS`) while the LAN host stays password-only.
- **Behind a proxy:** strip identity headers coming from outside (the example's
  `rundere-strip-ident` middleware). Nobody outside may assert who they are.
- **Exposure:** only the reverse proxy should reach the app (`expose:`, not
  `ports:`). That's what makes `TX_FORWARDED_ALLOW_IPS: "*"` safe.

## 5. Optional services

Everything works without them (D-146). Each is a setting in the example:

- **Email** (password resets, alerts): `TX_SMTP_*`.
- **Email-forward capture:** a mailbox read over IMAP. See [email-forward.md](email-forward.md).
- **File capture and attachment scanning:** a `clamav-rest` container (`TX_CLAMAV_URL`).
- **AI** (Ollama or any OpenAI-compatible API; off by default, SPEC-12), **geocoding** (Photon), **search** (SearXNG), **road routing** (Valhalla, openrouteservice or OSRM).
- **AirTrail / AdventureLog:** optional per-person connections, under Settings → Connections.

## 6. Backups

Back up these, and test a restore:

1. **The database** (`pg_dump`, or the volume while the database is stopped).
2. **The artifacts folder:** the original of every capture (D-15).
3. **The documents folder** (`TX_DOCUMENTS_DIR`), if you turned on file capture:
   uploaded PDFs, .ics and .eml files are stored there, not in the database.
4. **`TX_SECRETS_KEY`,** kept apart from the database backup.

**Leave the export folder out.** Account → Export my data writes each person's
archive to `TX_EXPORT_DIR` (by default `exports/` inside the artifacts folder,
so exclude that subfolder from the artifacts backup). An archive is one
person's whole record in a single zip with no password. Run·dere deletes it
after 7 days; a backup that copied it would keep it for as long as the backup
lives. Nothing is lost by skipping it: a person can build a new one at any
time. The folder is readable by the server's user only; don't serve it from a
web server or share it.

## 7. Upgrades

1. Read the release notes for migrations and new settings.
2. Back up.
3. Bump the image tag and redeploy. Migrations run at start-up.

Pin a version tag rather than `latest` if you want reproducible rollbacks.

**Upgrading to 5.0:** AI is now off unless `TX_AI_ENABLED=true`. To keep reading
booking emails with your LLM, set it, and move `TX_OLLAMA_URL` / `TX_OLLAMA_MODEL`
to `TX_AI_URL` / `TX_AI_MODEL_CHAT` (the old names still work in 5.0, with a warning).
See [SPEC-12](../SPEC-12-ai-providers.md).

## 8. A demo server

To run a read-only showcase like demo.rundere.app, see [demo-server.md](demo-server.md).

## 9. Deleting an account

**A person** deletes their own account under Account → Delete my account. They
confirm who they are (their password, or signing in again with single sign-on)
and type their username. The account is locked at once and deleted, with its
data, 7 days later; the 7 days are fixed. Until then, signing in on the web
shows the date, a "Cancel deletion" button and the export. The apps, API
tokens, connected apps, capture keys and the forwarding address stop working
at once, and cancelling doesn't bring them back. For each trip they own that
other accounts are on, they choose who it passes to, or that it is deleted;
those people are emailed.

**An admin** deletes another account from Users → Delete account…, by typing
its username. It takes the same path: locked at once, deleted after 7 days,
the person emailed. The person can't cancel an admin's deletion; an admin can.
The admin sees counts of trips, never their names, and shared trips pass to
someone else on them. The last active admin can't be deleted: make someone
else an admin first.

**From a shell**, which is also the way out for an account whose owner can't
sign in:

```bash
python -m app.cli delete-account <username>            # lock now, delete in 7 days
python -m app.cli delete-account <username> --dry-run  # counts per table, changes nothing
python -m app.cli delete-account <username> --now      # skip the 7 days
python -m app.cli delete-account <username> --cancel   # clear a pending deletion
```

**The deletion itself** runs in the daily retention job. It removes the
person's rows, their folders under `TX_DOCUMENTS_DIR` and `TX_EXPORT_DIR`, and
moves their forwarded emails in `Rundere/Captured` to the mailbox's Trash.
Their name stays on other people's trips as a guest with no account, and
expenses they entered on trips that go on stay, with no link to them.

**Out of reach.** Emails about a deletion are sent only when SMTP is set up;
without it the 7 days are the person's only protection against a stolen
password. A deletion doesn't touch your database backups (until they rotate),
server logs (which can hold a username or an id in a request path), mail
already in the mailbox's Trash, or anything in the person's own AirTrail or
AdventureLog. It never touches the single sign-on provider either: someone who
signs in through it afterwards gets a new, empty account, so remove them there
too if they shouldn't come back. A demo server, and a server with sign-in off,
have no account deletion.
