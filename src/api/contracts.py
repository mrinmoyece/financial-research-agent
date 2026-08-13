"""Validated API and queue contracts shared by the web and worker processes."""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ResearchRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=5,
        max_length=500,
        description="Natural language financial research question",
    )
    tickers: list[str] = Field(..., min_length=1, max_length=5)
    research_depth: Literal["quick", "standard", "deep"] = "standard"

    @field_validator("tickers")
    @classmethod
    def normalise_tickers(cls, value: list[str]) -> list[str]:
        tickers = list(dict.fromkeys(ticker.upper().strip() for ticker in value if ticker.strip()))
        if not tickers:
            raise ValueError("at least one non-blank ticker is required")
        invalid = [
            ticker for ticker in tickers if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker)
        ]
        if invalid:
            raise ValueError(f"invalid ticker symbol(s): {', '.join(invalid)}")
        return tickers


JobState = Literal[
    "pending",
    "running",
    "retrying",
    "awaiting_approval",
    "approved",
    "rejected",
    "completed",
    "failed",
]


class JobStatus(BaseModel):
    job_id: str
    status: JobState
    tenant_id: str
    principal_id: str
    created_at: str
    completed_at: str | None = None
    approved_at: str | None = None
    approved_by: str | None = None
    report: dict[str, object] | None = None
    error: str | None = None
    attempt: int = 0
    tool_calls_count: int = 0
    llm_calls_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
