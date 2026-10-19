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

1. Copy the example. Replace every `CHANGE_ME` and every `example.com` host.
2. Point the volumes at **local disk**, never NFS (Postgres corrupts on NFS).
   The app runs as an unprivileged user (uid and gid 10001). On its first start
   it hands its data folders (artifacts, documents, tile cache) to that user.
   Folders on a network share it can't change must already be writable by
   uid 10001. To keep the folders owned by your own account instead, run the
   container as you (`user: "1000:1000"` in the stack, with your uid and gid):
   it then skips the hand-over and still never runs as root.
3. Deploy the stack. Migrations run at start-up.
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
- **Local AI** (Ollama), **geocoding** (Photon), **search** (SearXNG), **road routing** (Valhalla, openrouteservice or OSRM).
- **AirTrail / AdventureLog:** optional per-person connections, under Settings → Connections.

## 6. Backups

Back up three things, and test a restore:

1. **The database** (`pg_dump`, or the volume while the database is stopped).
2. **The artifacts folder:** the original of every capture (D-15).
3. **`TX_SECRETS_KEY`,** kept apart from the database backup.

## 7. Upgrades

1. Read the release notes for migrations and new settings.
2. Back up.
3. Bump the image tag and redeploy. Migrations run at start-up.

Pin a version tag rather than `latest` if you want reproducible rollbacks.

## 8. A demo server

To run a read-only showcase like demo.rundere.app, see [demo-server.md](demo-server.md).
