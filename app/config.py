"""Config via env vars only — no hardcoded paths (HANDOFF Task 5).
Every value here is injected inline in the Portainer stack YAML in production
(Portainer cannot interpolate .env files) and by docker-compose.dev.yml in dev.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./yonder_dev.sqlite3"
    # Connection pool (Postgres only; SQLite keeps SQLAlchemy's defaults).
    # YONDER-178: a short checkout timeout makes an exhausted pool fail fast
    # with a 500 instead of stalling request threads for 30 s each.
    # Size + overflow = 40 matches the worker-thread pool that runs sync routes,
    # so a request thread never queues for a connection behind its siblings.
    db_pool_size: int = 20
    db_max_overflow: int = 20
    db_pool_timeout_seconds: int = 10
    db_pool_recycle_seconds: int = 1800
    # Load shedding (YONDER-178): past this many requests in flight, answer 503
    # + Retry-After at once instead of queueing. The server doesn't cancel work
    # for a client that hung up, so a client stuck in a retry loop would
    # otherwise build a backlog that takes minutes to drain. 0 = off.
    max_inflight_requests: int = 64
    artifacts_dir: str = "/data/artifacts"

    # /ingest hardening (HANDOFF: the security-critical surface)
    ingest_max_skew_seconds: int = 300      # signed-timestamp replay window (~5 min)
    ingest_rate_limit: int = 30             # requests per key per window
    ingest_rate_window_seconds: int = 300
    ingest_max_body_bytes: int = 5_000_000
    # SPEC-9 D-158: a capture on the capture page starts extract + resolve by itself.
    auto_process: bool = True

    # Extraction (D-11: local Ollama only; model choice = OQ-08)
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3:14b"
    llm_timeout_seconds: int = 600

    # Targets. Facts confirmed by the A4 probe live in the adapters.
    #
    # DEPRECATED (SPEC-8 D-150, YONDER-192): AirTrail and AdventureLog are now
    # per-user connections (Settings → Connections). These server-wide values
    # are read only to seed the deployment owner's connections on first boot
    # (app/integrations.py), and their hosts stay trusted for the outbound
    # policy while set. Remove them once the owner's connections exist.
    adventurelog_url: str = "http://adventurelog-server:80"
    adventurelog_api_key: str = ""
    # Browser-facing AdventureLog base (the one above is the internal container
    # address, not clickable). Used to build direct links to a collection —
    # {adventurelog_public_url}/collections/{id}. Empty = no link rendered.
    adventurelog_public_url: str = ""
    airtrail_url: str = "http://airtrail:3000"
    airtrail_token: str = ""

    # Per-user connection credentials are encrypted at rest (SPEC-8 D-151).
    # A key is 32 random bytes, base64 or hex (`openssl rand -base64 32`).
    # TX_SECRETS_KEYS="v2:<key>,v1:<key>" is a keyring for rotation (the first
    # encrypts, any decrypts); TX_SECRETS_KEY / TX_SECRETS_KEY_FILE (a Docker
    # secret) is one key named v1. No key = connections can't be saved. Back the
    # key up separately from the database.
    secrets_keys: str = ""
    secrets_key: str = ""
    secrets_key_file: str = ""
    # Outbound policy for user-supplied URLs (D-152): https and public addresses
    # only, except these hosts (host or host:port, comma-separated), which may be
    # on the LAN and use http — e.g. "airtrail:3000,adventurelog-server".
    integration_allowed_hosts: str = ""

    # Geocoding (D-22: Photon on travel-net, avoids Google Maps quota)
    photon_url: str = "http://photon:2322"
    # Road routing for a drive's distance, time and route line
    # (resolve/routing.py). Routers are tried in this order; one that isn't
    # configured is skipped, and one with no road data for the area (e.g. a
    # home-region Valhalla asked about India) falls through to the next — so
    # self-host for home and let a hosted API cover the rest of the world.
    routing_providers: str = "valhalla,ors,osrm"
    # Valhalla: the full /route endpoint — self-hosted
    # (http://valhalla:8002/route) or Stadia Maps
    # (https://api.stadiamaps.com/route/v1, with valhalla_api_key). Empty = off.
    valhalla_url: str = ""
    valhalla_api_key: str = ""
    # openrouteservice.org — worldwide, free key tier. Empty key = off.
    ors_api_key: str = ""
    ors_url: str = "https://api.openrouteservice.org"
    # OSRM: self-hosted, or the public demo server (fair use, driving only) as
    # the last resort. Empty = off.
    osrm_url: str = "https://router.project-osrm.org"
    # Map background for the PDF's trip maps (app/trips/maps.py): an XYZ raster
    # tile template. Default is OpenStreetMap's own tile server — fine for a
    # personal deployment's occasional PDF (cached 30 days, attributed, never
    # bulk); a public or busy instance must point this at its own TileServer GL
    # (e.g. http://tileserver:8080/styles/osm-bright/{z}/{x}/{y}{r}.png — {r}
    # asks for @2x tiles, crisper in print) or a hosted style. Empty = a plain
    # schematic background (lines + pins only).
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    map_tile_attribution: str = "© OpenStreetMap contributors"
    # Where fetched tiles are kept; empty = artifacts_dir/tile-cache (on the
    # artifacts volume, so it survives redeploys).
    map_tile_cache_dir: str = ""

    # Discovery (Yonder 3.0, SPEC-3-discovery.md). Deterministic-tier sources —
    # overridable so a deployment can self-host (e.g. its own Frankfurter) and
    # stay fully local. base_currencies is the deployment's ALWAYS-ON base
    # currencies the resolver + calculator convert FROM (D-65). They are unioned
    # with each trip traveller's own explicit pick (TravelerIdentity.base_currency)
    # — so a traveller's profile can be CAD while this keeps USD in view for a
    # closed-currency destination (e.g. Cuba). With no traveller pick, these are
    # the whole set. Defaults to USD (the universal reference) when unset — a
    # deployment sets this to its own home currencies (e.g. "CAD,USD").
    discovery_base_currencies: str = "USD"
    frankfurter_url: str = "https://api.frankfurter.dev/v2"
    open_meteo_url: str = "https://api.open-meteo.com/v1"
    open_meteo_archive_url: str = "https://archive-api.open-meteo.com/v1"
    # Discovery LLM tier (Yonder 3.2, SPEC-3.2 §5). SearXNG is already in the
    # stack (searxng-core on travel-net); its JSON API needs
    # `search.formats: [html, json]` in settings.yml. Empty = the LLM resolvers
    # degrade to absent findings (they require both searxng_url and ollama_url).
    searxng_url: str = "http://searxng:8080"

    # Auth (Yonder 4.0 Phase 1a, SPEC-4 Workstream B). Dependency-free HS256
    # JWT + scrypt (app/authn.py). auth_required is the master switch, ON by
    # default (SPEC-8 D-203): a server that forgets the setting asks for a
    # sign-in rather than opening everyone's trips. TX_AUTH_REQUIRED=false is
    # for a trusted single-user LAN only, and start-up warns about it.
    auth_required: bool = True
    # YONDER-181 / D-198: a read-only public demo server (app/demo.py). One shared
    # login; the password isn't a secret (the app's "Try the demo" fills it in).
    demo_mode: bool = False
    demo_username: str = "demo"
    demo_password: str = ""
    demo_timezone: str = "Europe/London"     # the nightly roll runs at 03:00 here
    # YONDER-126 (D-197): other Run·dere servers' /health URLs this server watches and
    # alerts on, comma-separated, e.g. https://demo.rundere.app/health. Set by the
    # operator in the stack, never by a user, so it isn't a user-supplied fetch (D-152).
    watch_urls: str = ""
    # YONDER-278 (D-202): the public About page and the sign-in page's "Get the app"
    # rows. All optional; an empty one hides its line. Only https:// links are used.
    ios_testflight_url: str = ""     # https://testflight.apple.com/join/…
    ios_app_store_url: str = ""      # replaces the TestFlight line once the app is public
    android_app_url: str = ""        # Play listing or test track; Android shows "Coming later" until set
    server_contact: str = ""         # an email or https URL: "Questions about this server" on About
    # HS256 signing key. MUST be set in prod (empty => a process-ephemeral key,
    # so tokens do not survive a restart — a loud dev-only fallback).
    jwt_secret: str = ""
    jwt_access_ttl_seconds: int = 1800              # mobile/API access token (30 min)
    jwt_refresh_ttl_seconds: int = 60 * 60 * 24 * 90  # mobile refresh token (90 days)
    jwt_web_ttl_seconds: int = 60 * 60 * 24 * 7     # web cookie session (7 days)
    jwt_api_ttl_seconds: int = 60 * 60 * 24 * 365   # long-lived machine token (MCP), 1yr
    auth_cookie_name: str = "yonder_session"
    # Secure cookie (https only). Prod is behind Traefik TLS => keep True; the
    # test stack talks plain http on :8140 => set TX_AUTH_COOKIE_SECURE=false.
    auth_cookie_secure: bool = True
    # Trusted reverse-proxy identity (D-113). OFF by default. When on, a request
    # carrying the proxy's identity header (e.g. Authentik's X-Authentik-Username)
    # auto-provisions/links a User — so an Authentik-fronted LAN needs no second
    # login. Only trust this where a proxy actually sets it and strips client copies.
    auth_proxy_header_trust: bool = False
    auth_proxy_header: str = "X-Authentik-Username"
    auth_proxy_auto_admin: bool = False             # never grant admin from a header by default

    # Onboarding (Phase 1a.1, D-117). First-run always lets you create the first
    # admin while the users table is empty; after that, registration is closed
    # unless this is turned on. Admins always create accounts from Administer →
    # Users regardless.
    auth_registration_open: bool = False
    password_reset_ttl_seconds: int = 3600          # reset-link validity (1 hour)
    # Password sign-in throttle (YONDER-293): failed attempts allowed per client
    # address AND per username in the window, web and API alike; then 429 until
    # the window passes. /forgot is limited per address so it can't spam inboxes.
    login_rate_limit: int = 10
    login_rate_window_seconds: int = 900
    forgot_rate_limit: int = 5
    # Optional SMTP for password-reset emails. Without it, /forgot tells the user
    # to contact an admin, and admins mint a copy-paste reset link from the Users
    # page. We email a reset LINK, never a password.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    # Implicit TLS from the first byte (SMTPS, usually port 465) instead of
    # upgrading a plain connection with STARTTLS (usually 587).
    smtp_ssl: bool = False
    # Email-forward capture (SPEC-9 D-162, D-190; YONDER-199). The admin owns one
    # mailbox that accepts plus-addressed mail; each user forwards confirmations
    # to `local+<their token>@domain`. Off until host, user and address are set.
    # Configured here like SMTP, so the mailbox password never enters the database.
    inbox_address: str = ""                  # e.g. trips@example.com — what users see
    inbox_imap_host: str = ""
    inbox_imap_port: int = 993
    inbox_imap_user: str = ""
    inbox_imap_password: str = ""
    inbox_imap_ssl: bool = True              # False: plain connection upgraded with STARTTLS
    # The server's certificate is always checked (YONDER-229). A self-signed one —
    # Proton Bridge's, for example — is trusted by pointing this at an exported
    # copy (PEM); only that certificate is then accepted.
    inbox_imap_ca_file: str = ""
    inbox_imap_verify: bool = True           # False: don't check at all (a link you trust)
    inbox_folder: str = "INBOX"              # where forwarded mail arrives
    inbox_done_folder: str = "Rundere/Captured"     # captured (or already captured) mail
    inbox_dropped_folder: str = "Rundere/Dropped"   # unknown address, too big, rate-limited
    # Push: hold an IMAP IDLE so new mail is read within seconds (D-190); when the
    # server can't IDLE, or this is off, read every inbox_poll_seconds instead.
    inbox_idle: bool = True
    inbox_poll_seconds: int = 120
    # A "got it" email to the capture's owner (their account address, never the
    # sender) as soon as it's captured. Needs SMTP; skipped without it.
    inbox_acknowledge: bool = True
    inbox_max_bytes: int = 10_000_000        # larger mail is dropped, not fetched
    inbox_rate_limit: int = 30               # captures per address per hour
    # YONDER-243: a clamav-rest service (e.g. http://clamav-rest:9000) that scans
    # every captured email, attachments included, before anything parses it.
    # Empty = no antivirus; the static PDF checks (extract/filescan.py) always run.
    clamav_url: str = ""
    # SPEC-8 D-196 (YONDER-179): the documents store — captured PDF/.ics/.eml files.
    # A local folder the hoster picks; empty = file capture off. ClamAV
    # (clamav_url) is required too: no unscanned file is ever stored.
    documents_dir: str = ""
    document_quota_mb: int = 1024            # per user; an admin can raise one user's
    document_max_mb: int = 25                # per file
    # Largest request body anyone may send (YONDER-293). Above the biggest file
    # upload plus form overhead; 0 = no limit (rely on the reverse proxy).
    max_request_mb: int = 32
    # D-196 retention: stored files and raw captures are removed this many months
    # after their trip ends (or after they arrived, with no trip); the inbox's
    # Dropped folder is moved to Trash after inbox_dropped_days. "" trash folder =
    # the mailbox's own (\Trash special-use) folder.
    retention_months: int = 12
    inbox_dropped_days: int = 30
    inbox_trash_folder: str = ""
    # Absolute base (e.g. https://rundere.example.com) for links in emails. Empty
    # => derive from the request that triggered the reset.
    public_base_url: str = ""

    # OIDC / external SSO (Phase 1b, D-118). Yonder is an OAuth2/OIDC *client*
    # (Authorization Code + PKCE) against Authentik/Keycloak/etc. First login
    # provisions a User (auth_source=oidc) and mints the SAME Yonder session —
    # one uniform token flow (D-111). OFF until issuer + client creds are set.
    oidc_enabled: bool = False
    oidc_issuer: str = ""                    # .../.well-known/openid-configuration lives under here
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid email profile"
    oidc_redirect_url: str = ""              # override; else {base}/oidc/callback
    oidc_display_name: str = "SSO"           # login-button label
    oidc_username_claim: str = "preferred_username"
    oidc_auto_create: bool = True            # provision a User on first SSO login
    oidc_admin_group: str = ""               # a `groups` claim value that grants admin (optional)
    # Per-hostname SSO (YONDER-164). Comma-separated host names, matched exactly
    # against the request's Host. Listing hosts also makes the OIDC callback URL
    # follow the host the user came in on (leave TX_OIDC_REDIRECT_URL empty).
    #   oidc_hosts        where "Sign in with SSO" is offered; empty = every host.
    #   oidc_force_hosts  SSO only: /login goes straight to the IdP and password
    #                     sign-in, registration and resets are refused there.
    # e.g. force the public name and keep the LAN name password-only, so the LAN
    # still works when the IdP is down.
    oidc_hosts: str = ""
    oidc_force_hosts: str = ""
    # Also refuse the app's password sign-in (/api/v1/auth/login) on forced
    # hosts. Off by default: the iOS app signs in with a password until it
    # can use SSO (YONDER-104), so turning this on locks it out away from home.
    oidc_force_api_login: bool = False

    # Yonder as an OAuth 2.1 authorization server (YONDER-107) — for the
    # claude.ai connector and other OAuth clients. Yonder always issues these
    # tokens itself; sign-in at /oauth/authorize is local, OIDC or proxy-header,
    # whatever the deployment has. Set TX_PUBLIC_BASE_URL in prod so the
    # metadata documents carry the public https URLs.
    oauth_access_ttl_seconds: int = 3600                    # access token (1 h)
    oauth_refresh_idle_ttl_seconds: int = 60 * 60 * 24 * 90  # grant dies after 90 idle days
    oauth_code_ttl_seconds: int = 60                        # authorization code lifetime
    oauth_register_rate_limit: int = 10                     # DCR registrations per IP…
    oauth_register_rate_window_seconds: int = 3600          # …per hour
    oauth_token_rate_limit: int = 60                        # /oauth/token calls per IP…
    oauth_token_rate_window_seconds: int = 60               # …per minute
    # The Run·dere iOS app's built-in OAuth client (D-194, YONDER-235): client
    # id `rundere-ios`, public (PKCE), on every server with no setup. Its
    # redirect URIs are https universal links (comma-separated); only a fork
    # that ships its own app bundle needs to change them. Empty = no app client.
    oauth_app_redirect_uris: str = "https://rundere.app/auth/callback"

    app_version: str = "4.12.6"

    # Docker secrets (YONDER-293): each secret below may be read from a file
    # (TX_JWT_SECRET_FILE=/run/secrets/jwt, …) instead of the environment, where
    # Portainer and `docker inspect` show it. A set *_FILE wins over the plain
    # setting; an unreadable one stops start-up rather than run without it.
    # TX_SECRETS_KEY_FILE predates these and keeps its own handling (D-151).
    jwt_secret_file: str = ""
    database_url_file: str = ""
    smtp_password_file: str = ""
    inbox_imap_password_file: str = ""
    oidc_client_secret_file: str = ""

    def model_post_init(self, __context) -> None:
        for name in ("jwt_secret", "database_url", "smtp_password",
                     "inbox_imap_password", "oidc_client_secret"):
            path = (getattr(self, f"{name}_file") or "").strip()
            if not path:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    value = fh.read().strip()
            except OSError:
                raise ValueError(f"TX_{name.upper()}_FILE can't be read") from None
            object.__setattr__(self, name, value)

    @property
    def watch_url_list(self) -> tuple[str, ...]:
        return tuple(u.strip() for u in self.watch_urls.split(",") if u.strip())

    @property
    def base_currency_list(self) -> tuple[str, ...]:
        return tuple(c.strip().upper() for c in self.discovery_base_currencies.split(",")
                     if c.strip())

    class Config:
        env_prefix = "TX_"                  # TX_DATABASE_URL, TX_INGEST_RATE_LIMIT, ...


settings = Settings()
