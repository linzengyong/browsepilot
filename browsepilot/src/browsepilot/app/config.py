"""V2 configuration. Env vars use the BP_ prefix to avoid clashing with v1."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BP_", env_file=".env", extra="ignore"
    )

    default_model: str = "deepseek/deepseek-chat"
    model_api_key: str = ""
    model_base_url: str = ""
    trace_dir: str = "data/traces"
    navigation_policy: str = "allow_https"  # allow_https | allowlist
    allowed_domains: list[str] = []
    session_timeout_seconds: int = 300
    max_steps: int = 30
    max_replans: int = 2
    consecutive_failures_threshold: int = 3
