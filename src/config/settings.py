"""
Application settings — loaded once at startup via pydantic-settings.
All sensitive values come from environment variables (never hard-coded).
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── LLM provider ──────────────────────────────────────────────
    llm_provider: Literal["azure_openai", "github_models", "openai"] = "azure_openai"

    # Azure OpenAI (prod)
    azure_openai_endpoint: str = ""
    azure_openai_api_key: SecretStr = SecretStr("")
    azure_openai_api_version: str = "2024-02-01"
    azure_chat_deployment: str = "gpt-4o"
    azure_embedding_deployment: str = "text-embedding-3-small"

    # GitHub Models (free dev/CI tier — same API shape as Azure OpenAI)
    github_models_endpoint: str = "https://models.inference.ai.azure.com"
    github_token: SecretStr = SecretStr("")
    github_chat_model: str = "gpt-4o"

    # OpenAI direct (fallback)
    openai_api_key: SecretStr = SecretStr("")
    openai_chat_model: str = "gpt-4o"

    # ── External data APIs ─────────────────────────────────────────
    alpha_vantage_api_key: SecretStr = SecretStr("")
    finnhub_api_key: SecretStr = SecretStr("")
    newsapi_api_key: SecretStr = SecretStr("")

    # ── Agent behaviour ───────────────────────────────────────────
    max_tool_retries: int = 3
    llm_temperature: float = 0.1       # low for financial analysis
    llm_max_tokens: int = 4096
    react_max_iterations: int = 15

    # ── Observability ─────────────────────────────────────────────
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "financial-research-agent"
    langsmith_tracing: bool = False     # set LANGCHAIN_TRACING_V2=true to enable

    # ── API server ────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    api_workers: int = 4

    # ── Redis (optional result cache) ─────────────────────────────
    redis_url: str = "redis://localhost:6379"
    cache_ttl_seconds: int = 3600


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton — safe to call from anywhere."""
    return Settings()
