"""Prompt-injection screening and deterministic bounds for untrusted content."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

_INJECTION_PATTERNS = (
    re.compile(r"\b(ignore|disregard|override)\b.{0,40}\b(instruction|prompt|system)\b", re.I),
    re.compile(r"\b(system|developer)\s+(message|prompt)\b", re.I),
    re.compile(r"\b(reveal|print|return)\b.{0,40}\b(secret|api key|credential|token)\b", re.I),
    re.compile(r"<\s*/?\s*(system|assistant|developer)\s*>", re.I),
    re.compile(r"\bdo not follow\b.{0,40}\b(previous|above)\b", re.I),
)


class UnsafeContentError(ValueError):
    """Raised when provider content contains instruction-like payloads."""


def contains_prompt_injection(text: str) -> bool:
    """Return whether text matches a conservative instruction-injection signature."""
    return any(pattern.search(text) is not None for pattern in _INJECTION_PATTERNS)


def sanitize_untrusted_text(
    text: str,
    *,
    max_chars: int,
    policy: Literal["block", "redact"],
) -> str:
    """Normalize, bound, and either block or redact instruction-like provider text."""
    normalized = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
    normalized = normalized[:max_chars]
    if not contains_prompt_injection(normalized):
        return normalized
    if policy == "block":
        raise UnsafeContentError("Untrusted content matched the prompt-injection policy")
    return "[CONTENT REDACTED: possible prompt injection]"


def sanitize_tool_result(
    value: Any,
    *,
    max_chars: int,
    policy: Literal["block", "redact"],
) -> Any:
    """Recursively sanitize strings and enforce a serialized output ceiling."""
    sanitized: Any
    if isinstance(value, str):
        return sanitize_untrusted_text(value, max_chars=max_chars, policy=policy)
    if isinstance(value, list):
        sanitized = [
            sanitize_tool_result(item, max_chars=max_chars, policy=policy) for item in value[:100]
        ]
    elif isinstance(value, dict):
        sanitized = {
            str(key)[:100]: sanitize_tool_result(item, max_chars=max_chars, policy=policy)
            for key, item in list(value.items())[:100]
        }
    else:
        return value

    serialized = json.dumps(sanitized, default=str)
    if len(serialized) > max_chars:
        raise UnsafeContentError("Untrusted tool output exceeded the configured content limit")
    return sanitized
