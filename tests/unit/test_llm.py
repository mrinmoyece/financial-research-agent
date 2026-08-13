"""Tests for provider selection and tracing configuration."""

import os
from unittest.mock import patch

from src.config.llm import build_llm, configure_langsmith
from src.config.settings import Settings


def test_builds_each_supported_provider():
    with patch("src.config.llm.AzureChatOpenAI") as azure:
        result = build_llm(
            Settings(
                llm_provider="azure_openai",
                azure_openai_endpoint="https://example.openai.azure.com",
                azure_openai_api_key="secret",
            )
        )
        assert result is azure.return_value
        azure.assert_called_once()
        assert azure.call_args.kwargs["api_key"] == "secret"

    with patch("src.config.llm.ChatOpenAI") as openai:
        result = build_llm(
            Settings(
                llm_provider="github_models",
                github_token="secret",
            )
        )
        assert result is openai.return_value
        assert openai.call_args.kwargs["base_url"].startswith("https://")
        assert openai.call_args.kwargs["api_key"] == "secret"

    with patch("src.config.llm.ChatOpenAI") as openai:
        result = build_llm(
            Settings(
                llm_provider="openai",
                openai_api_key="secret",
            )
        )
        assert result is openai.return_value
        assert "base_url" not in openai.call_args.kwargs
        assert openai.call_args.kwargs["api_key"] == "secret"


def test_configure_langsmith_sets_environment(monkeypatch):
    settings = Settings(
        langsmith_api_key="trace-key",
        langsmith_tracing=True,
        langsmith_project="project",
    )
    configure_langsmith(settings)
    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_API_KEY"] == "trace-key"
    assert os.environ["LANGCHAIN_PROJECT"] == "project"
    for key in ("LANGCHAIN_TRACING_V2", "LANGCHAIN_API_KEY", "LANGCHAIN_PROJECT"):
        os.environ.pop(key, None)
