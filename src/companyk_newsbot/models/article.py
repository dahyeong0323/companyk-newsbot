"""Canonical article representation shared by all future collectors."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Article(BaseModel):
    """A normalized news item before any routing or deduplication decision."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    canonical_url: str = Field(min_length=1)
    published_at: datetime | None = None
    retrieved_at: datetime
    description: str | None = None
    text: str | None = None
    language: str | None = None
    origin_metadata: dict[str, Any] = Field(default_factory=dict)
