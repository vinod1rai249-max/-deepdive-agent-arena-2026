from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App configuration, read from environment / .env. See .env.example for
    what every variable means. Fails fast (raises on construction) if
    MESH_API_KEY is missing — that's deliberate, not a bug."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mesh_api_key: str
    mesh_base_url: str = "https://api.meshapi.ai/v1"

    firecrawl_api_key: str | None = None

    database_url: str = "sqlite:///./deepdive.db"
    checkpoint_db_path: str = "./checkpoints.db"

    llm_cache_dir: str = "./.cache/llm"
    llm_cache_enabled: bool = True

    log_level: str = "INFO"

    budget_usd_per_run: float = 2.0
    budget_tokens_per_run: int = 500_000
    deadline_seconds_per_run: int = 600

    cb_failure_threshold: int = 4
    cb_cooldown_seconds: float = 60.0

    mesh_max_concurrency: int = 4

    # Context & memory budgets (BLUEPRINT.md §12) — keep any single prompt
    # and any single sub-question's evidence pool bounded as a run loops.
    max_passages_per_call: int = 12
    max_passages_per_subquestion: int = 30
    followup_top_k: int = 10
    relevance_prefilter_threshold: float = 0.05

    api_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
