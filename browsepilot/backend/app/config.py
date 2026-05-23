"""Application configuration via pydantic-settings."""

import json
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

# Explicit .env loading
ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    logger.warning(".env not found at {}", ENV_PATH)


class Settings(BaseSettings):
    openai_api_key: str = ""
    openai_base_url: str = "https://api.deepseek.com/v1"
    # Big model (main agent: plan/execute/reflect/replan/answer)
    big_model: str = "deepseek-v4-flash"
    big_model_api_key: str = ""   # empty = fallback to openai_api_key
    big_model_base_url: str = ""  # empty = fallback to openai_base_url

    # Small model (classify node)
    small_model: str = "deepseek-chat"
    small_model_api_key: str = ""   # empty = fallback to openai_api_key
    small_model_base_url: str = ""  # empty = fallback to openai_base_url

    llm_vision_enabled: bool = False
    default_search_url: str = "https://www.bing.com"
    max_turns_per_session: int = 10
    max_session_tokens: int = 100000
    browser_headless: bool = True
    browser_channel: str = ""
    browser_timeout: int = 15000
    allowed_domains: str = "github.com,baidu.com,wikipedia.org"
    log_level: str = "INFO"
    data_dir: str = "data"
    session_ttl_minutes: int = 60
    browser_pool_size: int = 8
    browser_pool_prewarm: int = 2
    browser_max_age_minutes: int = 30
    browser_max_requests: int = 50
    browser_idle_timeout: int = 10
    browser_acquire_timeout: float = 30.0
    mcp_tool_timeout: int = 30
    mcp_connect_retries: int = 3
    max_active_sessions: int = 10
    max_sessions_count: int = 100
    max_storage_mb: int = 500
    cleanup_interval_hours: int = 6
    llm_timeout_seconds: int = 60
    session_timeout_seconds: int = 300
    consecutive_failures_threshold: int = 3
    stagnation_threshold: int = 3
    replan_max_count: int = 2
    recursion_warning_threshold: int = 10
    max_context_tokens: int = 8000
    max_messages_count: int = 50

    # Tool classification
    tool_classify_threshold: int = Field(default=2, ge=1)

    # Step limits (dual-layer)
    plan_step_limit: int = Field(default=15, ge=1)
    total_step_limit: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def check_critical(self):
        if not self.openai_api_key.strip():
            raise ValueError(
                "OPENAI_API_KEY is required. "
                "Set it in .env or as an environment variable."
            )
        return self

    class Config:
        env_file = str(ENV_PATH)
        env_file_encoding = "utf-8"


settings = Settings()


def load_mcp_servers() -> dict:
    """Load MCP server configs from mcp_settings.json."""
    path = Path(__file__).resolve().parent.parent.parent / "mcp_settings.json"
    if not path.exists():
        logger.warning("mcp_settings.json not found at {}", path)
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("mcpServers", {})


def get_mcp_server_config(server_name: str = "browser-mcp") -> dict:
    """Get config for a specific MCP server."""
    servers = load_mcp_servers()
    if server_name not in servers:
        raise ValueError(f"MCP server '{server_name}' not found in mcp_settings.json")
    return servers[server_name]
