"""Shared Luna event-pair resolver with fail-separate audit metadata."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
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

    @classmethod
    def from_environment(cls) -> "LunaEventPairResolver":
        from openai import OpenAI
        return cls(
            OpenAI(timeout=float(os.getenv("LUNA_REQUEST_TIMEOUT_SECONDS", "30")), max_retries=int(os.getenv("OPENAI_MAX_RETRIES", "2"))),
            model=os.getenv("ROUTE_B_PRIMARY_MODEL", "gpt-5.6-luna").strip(),
            reasoning_effort=os.getenv("ROUTE_B_PRIMARY_REASONING", "medium").strip(),
        )

    def resolve(self, left: Article, right: Article) -> ResolverResult:
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
            if not isinstance(parsed, EventResolverOutput):
                return ResolverResult("DIFFERENT_EVENT", "malformed structured output; kept separate", True, "schema_failure")
            return ResolverResult(parsed.decision, parsed.short_reason, True)
        except Exception as exc:
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


def _article_payload(article: Article) -> dict[str, object]:
    return {
        "title": article.title,
        "description": article.description,
        "text": article.text,
        "source": article.source,
        "published_at": article.published_at.isoformat() if article.published_at else None,
    }
