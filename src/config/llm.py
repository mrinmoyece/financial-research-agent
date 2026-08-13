"""
LLM factory — returns a LangChain ChatModel bound to whichever provider
is configured.  Both AzureOpenAI and GitHub Models use the same
AzureChatOpenAI wrapper (GitHub Models exposes an OpenAI-compatible
endpoint), so switching providers requires no code change in agents.
"""

import logging
import os
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import AzureChatOpenAI, ChatOpenAI

from src.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


Provider = Literal["azure_openai", "github_models", "openai"]


def _build_provider_llm(cfg: Settings, provider: Provider) -> BaseChatModel:
    """Build one explicitly selected provider client."""
    if provider == "azure_openai":
        logger.info(
            "LLM: Azure OpenAI endpoint=%s deployment=%s",
            cfg.azure_openai_endpoint,
            cfg.azure_chat_deployment,
        )
        return AzureChatOpenAI(
            azure_endpoint=cfg.azure_openai_endpoint,
            api_key=cfg.azure_openai_api_key.get_secret_value(),
            api_version=cfg.azure_openai_api_version,
            azure_deployment=cfg.azure_chat_deployment,
            temperature=cfg.llm_temperature,
            max_completion_tokens=cfg.llm_max_tokens,
            max_retries=cfg.max_tool_retries,
        )
    if provider == "github_models":
        logger.info(
            "LLM: GitHub Models endpoint=%s model=%s",
            cfg.github_models_endpoint,
            cfg.github_chat_model,
        )
        return ChatOpenAI(
            base_url=cfg.github_models_endpoint,
            api_key=cfg.github_token.get_secret_value(),
            model=cfg.github_chat_model,
            temperature=cfg.llm_temperature,
            max_completion_tokens=cfg.llm_max_tokens,
            max_retries=cfg.max_tool_retries,
        )
    logger.info("LLM: OpenAI direct model=%s", cfg.openai_chat_model)
    return ChatOpenAI(
        api_key=cfg.openai_api_key.get_secret_value(),
        model=cfg.openai_chat_model,
        temperature=cfg.llm_temperature,
        max_completion_tokens=cfg.llm_max_tokens,
        max_retries=cfg.max_tool_retries,
    )


def build_llm(settings: Settings | None = None) -> BaseChatModel:
    """
    Returns a LangChain ChatModel instance.

    Resolution order:
      1. azure_openai  → AzureChatOpenAI (production default)
      2. github_models → AzureChatOpenAI pointed at GitHub Models endpoint
      3. openai        → ChatOpenAI direct (fallback)
    """
    cfg = settings or get_settings()

    return _build_provider_llm(cfg, cfg.llm_provider)


def build_llm_candidates(settings: Settings | None = None) -> list[BaseChatModel]:
    """Build the primary model followed by configured fallback providers."""
    cfg = settings or get_settings()
    providers: list[Provider] = [cfg.llm_provider, *cfg.fallback_providers]
    return [_build_provider_llm(cfg, provider) for provider in providers]


def configure_langsmith(settings: Settings | None = None) -> None:
    """Activate LangSmith tracing if LANGSMITH_API_KEY is set."""
    cfg = settings or get_settings()
    key = cfg.langsmith_api_key.get_secret_value()
    if key and cfg.langsmith_tracing:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = key
        os.environ["LANGCHAIN_PROJECT"] = cfg.langsmith_project
        logger.info("LangSmith tracing enabled project=%s", cfg.langsmith_project)
