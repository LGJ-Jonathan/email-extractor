"""Application settings. Mirrors .env.example (spec section 3)."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Auth / infra
    api_key: str = "change-me"
    database_url: str = "postgresql+asyncpg://app:app@postgres:5432/extractor"
    redis_url: str = "redis://redis:6379/0"

    # Fetching
    fetch_backend: Literal["jina", "httpx"] = "jina"
    jina_api_key: str = ""
    jina_rpm: int = 500
    jina_timeout_s: int = 10

    # TypeSafe
    typesafe_api_key: str = ""
    typesafe_model: str = "jev-latest"
    typesafe_concurrency: int = 10
    typesafe_confidence_min: float = 0.8
    typesafe_belongs_min: float = 0.5

    # Pipeline limits
    global_fetch_concurrency: int = 50
    per_domain_concurrency: int = 2
    max_pages_per_domain: int = 5
    max_page_bytes: int = 2_000_000
    max_page_tokens: int = 50_000        # X-Token-Budget: Jina refuses (409) at no cost
    # Spec 16.3 stops fetching once one good address is in hand. That is right when the
    # deliverable is a single best_email and wrong when it is every address on the site,
    # so it is a switch rather than a constant.
    early_stop_on_first_good: bool = False
    collect_all_emails: bool = True
    cache_days: int = 90
    connect_timeout_s: int = 3
    read_timeout_s: int = 7
    # 30s was sized when a domain fetched ~2 pages with no bucket contention. With the
    # early stop off (5 pages) and Jina paced at 500 rpm, a domain can spend most of its
    # budget queueing: 288 of 845 domains expired on the real list.
    #
    # 90 then became too tight in turn. It assumed a ~7s page, but the Jina call now
    # waits jina_timeout_s + JinaFetcher.CLIENT_MARGIN_S = 15s so that Jina's own
    # deadline expires first. Worst case went from ~35s (3 ladder rungs + robots +
    # sitemap) to ~75s before retries, and domain_timeout went 6x on the real list:
    # 18 of 491 domains against 3 in the run before it, costing 3 addresses outright.
    # Keep this above 2 x the worst-case ladder whenever the per-page budget moves.
    domain_timeout_s: int = 180          # hard wall-clock cap per domain (spec 16)
    max_domain_attempts: int = 3
    retry_pass_delay_s: int = 600        # delay before re-running transient failures (spec 17)
    user_agent: str = "Mozilla/5.0 (compatible; ContactFinder/1.0)"

    # Debug / ops
    debug_store_html: bool = False
    html_store_dir: str = "/data/html"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
