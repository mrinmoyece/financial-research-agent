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
    max_tool_retries: int = Field(3, ge=0, le=10)
    llm_temperature: float = Field(0.1, ge=0, le=2)
    llm_max_tokens: int = Field(4096, ge=256, le=32768)
    react_max_iterations: int = Field(15, ge=1, le=50)
    allow_mock_data: bool = True

    # ── Observability ─────────────────────────────────────────────
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "financial-research-agent"
    langsmith_tracing: bool = False  # set LANGCHAIN_TRACING_V2=true to enable

    # ── API server ────────────────────────────────────────────────
    api_host: str = "127.0.0.1"
    api_port: int = Field(8080, ge=1, le=65535)
    api_workers: int = Field(4, ge=1, le=32)
    api_auth_required: bool = False
    api_key: SecretStr = SecretStr("")
    max_request_body_bytes: int = Field(65536, ge=1024, le=1048576)
    cors_allow_origins: str = "http://localhost:3000,http://localhost:8080"
    expose_api_docs: bool = True

    # ── Redis (optional result cache) ─────────────────────────────
    redis_url: str = "redis://localhost:6379"
    redis_required: bool = False
    cache_ttl_seconds: int = Field(3600, ge=60, le=604800)

    @property
    def cors_origins(self) -> list[str]:
        """Return the configured, de-duplicated CORS origin allowlist."""
        return list(
            dict.fromkeys(
                origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()
            )
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton — safe to call from anywhere."""
    return Settings()
