"""Structured GPT-5.4 mini portfolio-company relevance gate."""
from __future__ import annotations

from enum import StrEnum
from dataclasses import dataclass
import json
import os
from time import monotonic
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


IDENTITY_SYSTEM_PROMPT = """Classify whether each article is actually about the named portfolio company. Use only supplied evidence. Lexical overlap alone is insufficient: a company-name word appearing as a common noun, geography, substring, or a different foreign product is NOT_RELATED. In particular, reject 봉선화 씨앗 and 평화의 씨앗, 식중독균 자란다, 서남해, 마이크로컨텍솔, and the unrelated Notta transcription product when the portfolio company is Korean computer-vision company 노타. Do not reject a genuine company article merely because its name is ambiguous. Return RELATED, NOT_RELATED, or UNCERTAIN."""


class IdentityVerdict(StrEnum):
    RELATED = "RELATED"
    NOT_RELATED = "NOT_RELATED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class IdentityDecision:
    verdict: IdentityVerdict
    confidence: str = "unknown"
    reason_code: str = "provider_unavailable"


class IdentityProvider(Protocol):
    def classify_many(self, *, company: str, aliases: list[str], registry_context: str,
                      articles: list[dict[str, object]]) -> dict[str, IdentityVerdict | IdentityDecision]: ...


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    article_id: str
    decision: IdentityVerdict
    confidence: str = Field(min_length=1, max_length=12)
    reason_code: str = Field(min_length=1, max_length=80)


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")
    articles: list[_Item]


class GPT54MiniIdentityProvider:
    model = "gpt-5.4-mini"

    def __init__(self, client: Any, *, retries: int = 1) -> None:
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
    def from_environment(cls) -> "GPT54MiniIdentityProvider":
        from openai import OpenAI
        return cls(OpenAI(timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))))

    def classify_many(self, *, company: str, aliases: list[str], registry_context: str,
                      articles: list[dict[str, object]]) -> dict[str, IdentityVerdict | IdentityDecision]:
        if not articles:
            return {}
        expected = {str(article["article_id"]) for article in articles}
        payload = {"company": company, "aliases": aliases, "registry_context": registry_context, "articles": articles}
        started = monotonic()
        for _ in range(self.retries + 1):
            self.requests += 1; self.attempts += 1
            try:
                response = self.client.responses.parse(
                    model=self.model, reasoning={"effort": "low"}, text_format=_Response,
                    input=[
                        {"role": "system", "content": IDENTITY_SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                )
                self._record_usage(response)
                parsed = response.output_parsed
                values = {
                    item.article_id: IdentityDecision(item.decision, item.confidence, item.reason_code)
                    for item in parsed.articles
                } if isinstance(parsed, _Response) else {}
                if set(values) == expected and len(values) == len(articles):
                    self.latencies_ms.append((monotonic() - started) * 1000)
                    return values
            except Exception:
                pass
        self.failures += 1; self.latencies_ms.append((monotonic() - started) * 1000)
        return {article_id: IdentityDecision(IdentityVerdict.UNCERTAIN, "unknown", "provider_failure") for article_id in expected}

    def metrics_payload(self) -> dict[str, object]:
        ordered = sorted(self.latencies_ms)
        p50 = ordered[(len(ordered) - 1) // 2] if ordered else 0
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * .95))] if ordered else 0
        return {"identity_requests": self.requests, "identity_attempts": self.attempts,
                "identity_failures": self.failures, "identity_latency_p50_ms": round(p50, 2),
                "identity_latency_p95_ms": round(p95, 2), "identity_input_tokens": self.input_tokens,
                "identity_cached_input_tokens": self.cached_input_tokens,
                "identity_output_tokens": self.output_tokens, "identity_reasoning_tokens": self.reasoning_tokens}
