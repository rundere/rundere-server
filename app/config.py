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

    # Discovery (Yonder 3.0, SPEC-3-discovery.md). Deterministic-tier sources —
    # overridable so a deployment can self-host (e.g. its own Frankfurter) and
    # stay fully local. base_currencies is the home bases the currency resolver +
    # calculator convert FROM (D-65).
    discovery_base_currencies: str = "CAD,USD"
    frankfurter_url: str = "https://api.frankfurter.dev/v1"
    open_meteo_url: str = "https://api.open-meteo.com/v1"
    open_meteo_archive_url: str = "https://archive-api.open-meteo.com/v1"
    # Discovery LLM tier (Yonder 3.2, SPEC-3.2 §5). SearXNG is already in the
    # stack (searxng-core on travel-net); its JSON API needs
    # `search.formats: [html, json]` in settings.yml. Empty = the LLM resolvers
    # degrade to absent findings (they require both searxng_url and ollama_url).
    searxng_url: str = "http://searxng:8080"

    app_version: str = "3.8.1"

    @property
    def base_currency_list(self) -> tuple[str, ...]:
        return tuple(c.strip().upper() for c in self.discovery_base_currencies.split(",")
                     if c.strip())

    class Config:
        env_prefix = "TX_"                  # TX_DATABASE_URL, TX_INGEST_RATE_LIMIT, ...


settings = Settings()
