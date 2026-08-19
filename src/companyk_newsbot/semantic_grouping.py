"""Structured GPT-5.4 mini per-company event partitioning."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from time import monotonic
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class EventCandidate:
    article_id: str
    title: str
    lead: str
    publisher: str
    published_at: datetime | None


@dataclass(frozen=True)
class EventGroup:
    member_article_ids: tuple[str, ...]
    representative_article_id: str
    event_label: str
    reason: str = ""
    representative_reason: str = ""


class GroupingProvider(Protocol):
    def partition(self, *, company_id: str, candidates: list[EventCandidate]) -> tuple[EventGroup, ...]: ...


def validate_partition(candidates: list[EventCandidate], groups: tuple[EventGroup, ...]) -> tuple[EventGroup, ...]:
    expected = {candidate.article_id for candidate in candidates}
    members = [member for group in groups for member in group.member_article_ids]
    if set(members) != expected or len(members) != len(set(members)):
        raise ValueError("grouping must partition each article ID exactly once")
    for group in groups:
        if not group.event_label.strip() or group.representative_article_id not in group.member_article_ids:
            raise ValueError("grouping has invalid representative or event label")
    return groups


class _WireGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_article_ids: list[str] = Field(min_length=1)
    representative_article_id: str
    event_label: str = Field(min_length=1, max_length=160)
    reason: str = Field(default="", max_length=200)
    representative_reason: str = Field(default="", max_length=200)


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groups: list[_WireGroup]


class GPT54MiniGroupingProvider:
    model = "gpt-5.4-mini"

    def __init__(self, client: Any, *, retries: int = 0) -> None:
        self.client, self.retries = client, retries
        self.requests = self.attempts = self.failures = 0
        self.latencies_ms: list[float] = []
        self.input_tokens = self.cached_input_tokens = self.output_tokens = self.reasoning_tokens = 0

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        if not isinstance(usage, dict):
            return
        self.input_tokens += int(usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)
        input_details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        if isinstance(input_details, dict):
            self.cached_input_tokens += int(input_details.get("cached_tokens") or 0)
        if isinstance(output_details, dict):
            self.reasoning_tokens += int(output_details.get("reasoning_tokens") or 0)

    @classmethod
    def from_environment(cls) -> "GPT54MiniGroupingProvider":
        from openai import OpenAI
        return cls(OpenAI(timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))))

    def partition(self, *, company_id: str, candidates: list[EventCandidate]) -> tuple[EventGroup, ...]:
        payload = {"company_id": company_id, "articles": [
            {"article_id": c.article_id, "title": c.title, "lead": c.lead, "publisher": c.publisher,
             "published_at": c.published_at.isoformat() if c.published_at else None} for c in candidates]}
        started = monotonic()
        for _ in range(self.retries + 1):
            self.requests += 1; self.attempts += 1
            try:
                response = self.client.responses.parse(
                    model=self.model, reasoning={"effort": "low"}, text_format=_Response,
                    input=[
                        {"role": "system", "content": "Partition one portfolio company's articles into underlying real-world events. Every article ID must occur exactly once. Do not merge merely similar company news. Pick one supplied representative per group: prefer an official release or reputable direct report with the richest concrete facts; avoid personal blogs, reposts, and low-quality aggregators when a better supplied source exists. Give a short grouping reason and representative reason."},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                )
                self._record_usage(response)
                parsed = response.output_parsed
                if isinstance(parsed, _Response):
                    groups = tuple(EventGroup(tuple(g.member_article_ids), g.representative_article_id, g.event_label, g.reason, g.representative_reason) for g in parsed.groups)
                    self.latencies_ms.append((monotonic() - started) * 1000)
                    return validate_partition(candidates, groups)
            except Exception:
                pass
        self.failures += 1; self.latencies_ms.append((monotonic() - started) * 1000)
        return tuple()

    def metrics_payload(self) -> dict[str, object]:
        ordered = sorted(self.latencies_ms)
        p50 = ordered[(len(ordered) - 1) // 2] if ordered else 0
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * .95))] if ordered else 0
        return {"grouping_requests": self.requests, "grouping_attempts": self.attempts,
                "grouping_failures": self.failures, "grouping_latency_p50_ms": round(p50, 2),
                "grouping_latency_p95_ms": round(p95, 2), "grouping_input_tokens": self.input_tokens,
                "grouping_cached_input_tokens": self.cached_input_tokens,
                "grouping_output_tokens": self.output_tokens, "grouping_reasoning_tokens": self.reasoning_tokens}
