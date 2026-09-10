"""V2 configuration. Env vars use the BP_ prefix to avoid clashing with v1."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BP_", env_file=".env", extra="ignore")

    default_model: str = "deepseek/deepseek-chat"
    model_api_key: str = ""
    model_base_url: str = ""
    trace_dir: str = "data/traces"
    session_db_path: str = "data/sessions.sqlite"
    checkpoint_db_path: str = "data/checkpoints.sqlite"
    navigation_policy: str = "allow_https"  # allow_https | allowlist
    allowed_domains: list[str] = []
    session_timeout_seconds: int = 300
    max_steps: int = 30
    max_replans: int = 2
    consecutive_failures_threshold: int = 3

    # ── P2 生产安全层：限流 + 会话成本预算 ──
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60
    session_budget_tokens: int = 200000

    # ── P3 Tier3 浏览器层（BrowserPool；start/close 由 app lifespan 管） ──
    browser_headless: bool = True
    browser_max_pages: int = 3
    # 浏览器通道（BP_BROWSER_CHANNEL）：空 = Playwright 自带 Chromium；"msedge"/"chrome"
    # = 用系统浏览器。本机 PW 1.59 默认要的 chromium rev 在本机缓存里不存在，所以真实环境
    # 靠它逃生（T6 §5.6）；空串在接线处收敛成 None（channel="" 不是合法值）。
    browser_channel: str = ""
