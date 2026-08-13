"""
Application settings — loaded once at startup via pydantic-settings.
All sensitive values come from environment variables (never hard-coded).
"""

from functools import lru_cache
from typing import Literal, cast

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
    llm_fallback_providers: str = ""

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
    max_model_calls_per_job: int = Field(20, ge=1, le=100)
    max_tool_calls_per_job: int = Field(30, ge=1, le=200)
    max_external_content_chars: int = Field(12000, ge=1000, le=100000)
    prompt_injection_policy: Literal["block", "redact"] = "block"
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
    api_principals_json: SecretStr = SecretStr("")
    rate_limit_requests_per_minute: int = Field(60, ge=1, le=10000)
    max_concurrent_jobs_per_tenant: int = Field(10, ge=1, le=1000)
    require_report_approval: bool = True
    max_request_body_bytes: int = Field(65536, ge=1024, le=1048576)
    cors_allow_origins: str = "http://localhost:3000,http://localhost:8080"
    expose_api_docs: bool = True

    # ── Redis (optional result cache) ─────────────────────────────
    redis_url: str = "redis://localhost:6379"
    redis_required: bool = False
    cache_ttl_seconds: int = Field(3600, ge=60, le=604800)
    job_retention_seconds: int = Field(2592000, ge=86400, le=31536000)
    job_max_attempts: int = Field(3, ge=1, le=10)
    job_lease_seconds: int = Field(300, ge=30, le=3600)
    job_poll_timeout_seconds: int = Field(5, ge=1, le=30)
    circuit_breaker_failure_threshold: int = Field(3, ge=1, le=20)
    circuit_breaker_recovery_seconds: int = Field(60, ge=5, le=3600)

    @property
    def cors_origins(self) -> list[str]:
        """Return the configured, de-duplicated CORS origin allowlist."""
        return list(
            dict.fromkeys(
                origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()
            )
        )

    @property
    def fallback_providers(self) -> list[Literal["azure_openai", "github_models", "openai"]]:
        allowed = {"azure_openai", "github_models", "openai"}
        providers = [
            provider.strip()
            for provider in self.llm_fallback_providers.split(",")
            if provider.strip()
        ]
        invalid = set(providers) - allowed
        if invalid:
            raise ValueError(f"Unsupported LLM fallback provider(s): {sorted(invalid)}")
        return [
            cast(Literal["azure_openai", "github_models", "openai"], provider)
            for provider in providers
            if provider != self.llm_provider
        ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton — safe to call from anywhere."""
    return Settings()
