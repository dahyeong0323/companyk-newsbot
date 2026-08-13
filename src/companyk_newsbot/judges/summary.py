"""Structured user-facing summaries generated only after qualification."""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from companyk_newsbot.ranking import RankedNewsItem


SUMMARY_PROMPT_VERSION = "news_summary_v1"
SUMMARY_SYSTEM_PROMPT = """Write a concise, factual Korean news summary for an investment-team email.
Use only supplied article and approved qualification context. Do not expose exposure IDs,
scores, prompts, or internal process details. The summary states what happened in 1–2
short sentences. For an external-impact item, add a company-specific why_it_matters in
one short sentence based on the approved causal mechanism. Do not add facts."""


SUMMARY_SYSTEM_PROMPT = """Write a concise Korean factual event summary and a one-sentence executive or investment insight.
Use only supplied evidence and approved qualification context. Do not invent valuation, ownership, runway,
revenue, market share, probability, timeline, or causal claims. If no defensible implication follows, use
insight_mode=watchpoint and identify the concrete next variable. Return only supplied evidence_article_ids.
Every output requires summary, insight_one_liner, insight_dimension, insight_mode, confidence, and evidence_article_ids."""


class SummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=500)
    why_it_matters: str | None = Field(default=None, max_length=350)
    insight_one_liner: str | None = Field(default=None, min_length=1, max_length=500)
    insight_dimension: Literal["exit_liquidity", "financing_runway", "valuation_comps", "revenue_traction", "regulatory_clinical", "commercialization", "cost_supply", "customer_platform", "competition", "governance", "strategy", "other"] | None = None
    insight_mode: Literal["implication", "watchpoint"] | None = None
    confidence: Literal["high", "medium"] | None = None
    evidence_article_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def external_summary_requires_why(self) -> "SummaryOutput":
        if self.insight_one_liner and (self.insight_dimension is None or self.insight_mode is None or self.confidence is None):
            raise ValueError("insight requires dimension, mode, and confidence")
        return self


class SummaryError(RuntimeError):
    """Raised when an LLM response cannot be used as a user-facing summary."""


class ResponsesParser(Protocol):
    def parse(self, **kwargs: Any) -> Any: ...


class SummaryClient(Protocol):
    responses: ResponsesParser


class NewsSummarizer:
    def __init__(self, client: SummaryClient, *, model: str, reasoning_effort: str = "medium") -> None:
        if not model.strip():
            raise ValueError("OpenAI model must not be blank")
        self._client = client
        self.model = model
        self.reasoning_effort = reasoning_effort

    def summarize(self, item: RankedNewsItem) -> SummaryOutput:
        response = self._client.responses.parse(
            model=self.model,
            input=[
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": self._payload(item)},
            ],
            text_format=SummaryOutput,
            reasoning={"effort": self.reasoning_effort},
        )
        parsed = getattr(response, "output_parsed", None)
        if not isinstance(parsed, SummaryOutput):
            raise SummaryError("OpenAI response did not contain a parsed news summary")
        if item.route == "external" and not parsed.why_it_matters:
            raise SummaryError("external-impact summaries must include why_it_matters")
        if item.route == "direct" and parsed.why_it_matters:
            raise SummaryError("direct-company summaries must not include why_it_matters")
        if not parsed.insight_one_liner:
            raise SummaryError("every final event requires an executive insight")
        from companyk_newsbot.dedup.event import article_id
        article = item.direct_match.article if item.direct_match else item.external_match.candidate.article
        if not parsed.evidence_article_ids or not set(parsed.evidence_article_ids).issubset({article_id(article)}):
            raise SummaryError("summary returned an unknown evidence article ID")
        return parsed

    @staticmethod
    def _payload(item: RankedNewsItem) -> str:
        context: dict[str, object] = {
            "route": item.route,
            "company": item.company,
            "materiality": item.materiality,
            "article": {"title": item.article_title, "url": item.article_url},
        }
        if item.direct_match:
            from companyk_newsbot.dedup.event import article_id
            context["article"]["article_id"] = article_id(item.direct_match.article)
            context["article"].update(
                {"description": item.direct_match.article.description, "text": item.direct_match.article.text}
            )
        if item.external_match:
            from companyk_newsbot.dedup.event import article_id
            context["article"]["article_id"] = article_id(item.external_match.candidate.article)
            decision = item.external_match.decision
            context["approved_external_impact"] = {
                "event_family": decision.event_family,
                "impact_direction": decision.impact_direction,
                "causal_mechanism": decision.causal_mechanism,
                "article_description": item.external_match.candidate.article.description,
                "article_text": item.external_match.candidate.article.text,
            }
        return json.dumps(context, ensure_ascii=False)
