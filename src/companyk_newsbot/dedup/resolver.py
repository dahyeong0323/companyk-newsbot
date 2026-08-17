"""Shared Luna event-pair resolver with fail-separate audit metadata."""
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
import json
import os
from time import monotonic
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from companyk_newsbot.models import Article


class EventResolverOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["SAME_EVENT", "DIFFERENT_EVENT"]
    short_reason: str = Field(min_length=1, max_length=300)


@dataclass(frozen=True)
class ResolverResult:
    decision: Literal["SAME_EVENT", "DIFFERENT_EVENT"]
    short_reason: str
    invoked: bool
    failure_type: str | None = None


class EventPairResolver(Protocol):
    def resolve(self, left: Article, right: Article) -> ResolverResult: ...


class LunaEventPairResolver:
    def __init__(self, client: Any, *, model: str = "gpt-5.6-luna", reasoning_effort: str = "medium") -> None:
        self.client, self.model, self.reasoning_effort = client, model, reasoning_effort
        self.calls = 0
        self.failures = 0
        self.latencies_ms: list[float] = []
        self.usage: Counter[str] = Counter()

    @classmethod
    def from_environment(cls) -> "LunaEventPairResolver":
        from openai import OpenAI
        model = os.getenv("EVENT_RESOLVER_MODEL", "gpt-5.6-luna").strip()
        if os.getenv("NEWSBOT_COST_FIRST_PIPELINE", "true").strip().casefold() in {"1", "true", "yes", "on"} and "sol" in model.casefold():
            raise ValueError("cost-first event resolver must not use Sol")
        return cls(
            OpenAI(timeout=float(os.getenv("LUNA_REQUEST_TIMEOUT_SECONDS", "30")), max_retries=int(os.getenv("OPENAI_MAX_RETRIES", "2"))),
            model=model,
            reasoning_effort=os.getenv("EVENT_RESOLVER_REASONING", "low").strip(),
        )

    def resolve(self, left: Article, right: Article) -> ResolverResult:
        started = monotonic()
        self.calls += 1
        try:
            response = self.client.responses.parse(
                model=self.model,
                reasoning={"effort": self.reasoning_effort},
                text_format=EventResolverOutput,
                input=[
                    {"role": "system", "content": "Decide whether these articles describe the SAME underlying real-world event, not merely the same company or topic. SAME_EVENT means one is redundant coverage in a morning brief. If evidence is insufficient choose DIFFERENT_EVENT. Use only supplied evidence."},
                    {"role": "user", "content": json.dumps({"left": _article_payload(left), "right": _article_payload(right)}, ensure_ascii=False)},
                ],
            )
            parsed = getattr(response, "output_parsed", None)
            self._record_usage(response)
            if not isinstance(parsed, EventResolverOutput):
                self.failures += 1
                return ResolverResult("DIFFERENT_EVENT", "malformed structured output; kept separate", True, "schema_failure")
            return ResolverResult(parsed.decision, parsed.short_reason, True)
        except Exception as exc:
            self.failures += 1
            name = type(exc).__name__.casefold()
            status_code = getattr(exc, "status_code", None)
            if "timeout" in name:
                failure = "timeout"
            elif status_code == 429:
                failure = "rate_limit_exhausted"
            elif isinstance(status_code, int) and status_code >= 500:
                failure = "server_error_exhausted"
            else:
                failure = "client_error"
            return ResolverResult("DIFFERENT_EVENT", "resolver failure; kept separate", True, failure)
        finally:
            self.latencies_ms.append((monotonic() - started) * 1000)

    def metrics_payload(self) -> dict[str, object]:
        ordered = sorted(self.latencies_ms)
        p50 = ordered[(len(ordered) - 1) // 2] if ordered else 0
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * .95))] if ordered else 0
        return {
            "event_resolver_requests": self.calls,
            "event_resolver_failures": self.failures,
            "event_resolver_input_tokens": self.usage["input_tokens"],
            "event_resolver_cached_input_tokens": self.usage["cached_input_tokens"],
            "event_resolver_output_tokens": self.usage["output_tokens"],
            "event_resolver_reasoning_tokens": self.usage["reasoning_tokens"],
            "event_resolver_latency_p50_ms": round(p50, 2),
            "event_resolver_latency_p95_ms": round(p95, 2),
        }

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        for name in ("input_tokens", "output_tokens"):
            value = getattr(usage, name, None) if usage else None
            if isinstance(value, int):
                self.usage[name] += value
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", None) if usage else None
        if isinstance(cached, int):
            self.usage["cached_input_tokens"] += cached
        reasoning = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", None) if usage else None
        if isinstance(reasoning, int):
            self.usage["reasoning_tokens"] += reasoning


def _article_payload(article: Article) -> dict[str, object]:
    return {
        "title": article.title,
        "description": article.description,
        "text": article.text,
        "source": article.source,
        "published_at": article.published_at.isoformat() if article.published_at else None,
    }
