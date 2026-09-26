"""Config via env vars only — no hardcoded paths (HANDOFF Task 5).
Every value here is injected inline in the Portainer stack YAML in production
(Portainer cannot interpolate .env files) and by docker-compose.dev.yml in dev.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./yonder_dev.sqlite3"
    artifacts_dir: str = "/data/artifacts"

    # /ingest hardening (HANDOFF: the security-critical surface)
    ingest_max_skew_seconds: int = 300      # signed-timestamp replay window (~5 min)
    ingest_rate_limit: int = 30             # requests per key per window
    ingest_rate_window_seconds: int = 300
    ingest_max_body_bytes: int = 5_000_000

    # Extraction (D-11: local Ollama only; model choice = OQ-08)
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3:14b"
    llm_timeout_seconds: int = 600

    # Targets. Facts confirmed by the A4 probe live in the adapters.
    adventurelog_url: str = "http://adventurelog-server:80"
    adventurelog_api_key: str = ""
    # Browser-facing AdventureLog base (the one above is the internal container
    # address, not clickable). Used to build direct links to a collection —
    # {adventurelog_public_url}/collections/{id}. Empty = no link rendered.
    adventurelog_public_url: str = ""
    airtrail_url: str = "http://airtrail:3000"
    airtrail_token: str = ""
    # AirTrail's own Postgres (READ-ONLY) — its `airport`/`airline` tables are the
    # only complete source of airport ids/coords/tz for airports never flown
    # before, which the flight-save endpoint needs (D-50). Optional: unset falls
    # back to the flown-airport reference. e.g.
    # postgresql://airtrail:...@airtrail_db:5432/airtrail
    airtrail_db_url: str = ""

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
    # JWT + scrypt (app/authn.py). auth_required is the master switch: OFF by
    # default so existing single-user deployments keep working untouched until
    # the operator creates a user and opts in (TX_AUTH_REQUIRED=true). The 4.0
    # test stack turns it ON.
    auth_required: bool = False
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
    # Absolute base (e.g. https://yonder.home.example) for links in emails. Empty
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

    app_version: str = "4.3.1"

    @property
    def base_currency_list(self) -> tuple[str, ...]:
        return tuple(c.strip().upper() for c in self.discovery_base_currencies.split(",")
                     if c.strip())

    class Config:
        env_prefix = "TX_"                  # TX_DATABASE_URL, TX_INGEST_RATE_LIMIT, ...


settings = Settings()
