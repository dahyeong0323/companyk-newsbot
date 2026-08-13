"""Evidence-grounded final event editor, invoked only after ranking."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from companyk_newsbot.dedup.event import article_id
from companyk_newsbot.dedup.representative import RepresentativeArticleSelector
from companyk_newsbot.models import Article
from companyk_newsbot.ranking import RankedNewsItem

SUMMARY_PROMPT_VERSION = "grounded_event_editor_v2"
SUMMARY_SYSTEM_PROMPT = """Write one concise Korean factual summary and one executive/investment insight for the supplied ranked event.
Use only supplied evidence. Do not introduce valuation, ownership, runway, revenue, market share, probability,
timeline, or causal claims unless directly supported or logically entailed by approved context. Analytical value
does not permit inventing facts. If a defensible implication is not supported, use insight_mode=watchpoint and
state the concrete variable or milestone an investor should monitor next. Never invent a stronger implication to
avoid watchpoint mode. Return only supplied article IDs in evidence_article_ids."""


class SummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_summary: str = Field(min_length=1, max_length=500)
    why_it_matters: str | None = Field(default=None, max_length=350)
    insight_one_liner: str = Field(min_length=1, max_length=500)
    insight_dimension: Literal["exit_liquidity", "financing_runway", "valuation_comps", "revenue_traction", "regulatory_clinical", "commercialization", "cost_supply", "customer_platform", "competition", "governance", "strategy", "other"]
    insight_mode: Literal["implication", "watchpoint"]
    confidence: Literal["high", "medium"]
    evidence_article_ids: list[str] = Field(min_length=1, max_length=4)

    @property
    def summary(self) -> str:
        return self.fact_summary


class SummaryError(RuntimeError):
    pass


class ResponsesParser(Protocol):
    def parse(self, **kwargs: Any) -> Any: ...


class SummaryClient(Protocol):
    responses: ResponsesParser


@dataclass
class SummaryMetrics:
    calls: int = 0
    evidence_retries: int = 0
    failures: int = 0
    implication_count: int = 0
    watchpoint_count: int = 0

    def payload(self) -> dict[str, int]:
        return {
            "summary_calls": self.calls,
            "summary_evidence_retries": self.evidence_retries,
            "summary_failures": self.failures,
            "insight_implication_count": self.implication_count,
            "insight_watchpoint_count": self.watchpoint_count,
        }


class NewsSummarizer:
    def __init__(self, client: SummaryClient, *, model: str, reasoning_effort: str = "medium") -> None:
        if not model.strip():
            raise ValueError("OpenAI model must not be blank")
        self._client, self.model, self.reasoning_effort = client, model, reasoning_effort
        self.metrics = SummaryMetrics()

    def summarize(self, item: RankedNewsItem) -> SummaryOutput:
        payload, valid_ids = self._payload(item)
        corrective = ""
        for attempt in range(2):
            self.metrics.calls += 1
            try:
                response = self._client.responses.parse(
                    model=self.model,
                    input=[
                        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                        {"role": "user", "content": payload + corrective},
                    ],
                    text_format=SummaryOutput,
                    reasoning={"effort": self.reasoning_effort},
                )
            except Exception as exc:
                self.metrics.failures += 1
                raise SummaryError("final editor request or structured output failed safely") from exc
            parsed = getattr(response, "output_parsed", None)
            if not isinstance(parsed, SummaryOutput):
                self.metrics.failures += 1
                raise SummaryError("OpenAI response did not contain a parsed event summary")
            shape_error = self._shape_error(item, parsed)
            evidence_valid = set(parsed.evidence_article_ids).issubset(valid_ids)
            if shape_error is None and evidence_valid:
                if parsed.insight_mode == "implication": self.metrics.implication_count += 1
                else: self.metrics.watchpoint_count += 1
                return parsed
            if not evidence_valid and attempt == 0:
                self.metrics.evidence_retries += 1
                corrective = "\nCORRECTION: evidence_article_ids must contain only IDs supplied in the event payload. Return a corrected complete result."
                continue
            self.metrics.failures += 1
            raise SummaryError(shape_error or "summary returned an unknown evidence article ID after one retry")
        raise AssertionError("unreachable")

    @staticmethod
    def _shape_error(item: RankedNewsItem, parsed: SummaryOutput) -> str | None:
        if item.route == "external" and not parsed.why_it_matters:
            return "external-impact summaries must include why_it_matters"
        if item.route == "direct" and parsed.why_it_matters:
            return "direct-company summaries must not include why_it_matters"
        return None

    @staticmethod
    def _article_payload(article: Article) -> dict[str, object]:
        return {
            "article_id": article_id(article), "title": article.title, "url": article.canonical_url,
            "source": article.source, "published_at": article.published_at.isoformat() if article.published_at else None,
            "description": article.description, "text": article.text,
        }

    @classmethod
    def _payload(cls, item: RankedNewsItem) -> tuple[str, set[str]]:
        selector = RepresentativeArticleSelector()
        if item.direct_event:
            representative = item.direct_event.primary.article
            alternatives = [match.article for match in item.direct_event.coverage]
        elif item.external_event:
            representative = item.external_event.representative.candidate.article
            alternatives = [article for article in item.external_event.all_articles if article_id(article) != article_id(representative)]
        else:
            representative = item.direct_match.article if item.direct_match else item.external_match.candidate.article
            alternatives = []
        corroborating = selector.corroborating(representative, alternatives, limit=3)
        evidence = (representative, *corroborating)
        context: dict[str, object] = {
            "route": item.route,
            "event_id": item.event_id,
            "company": item.company,
            "impacted_companies": list(item.impacted_companies),
            "event_materiality": item.materiality,
            "representative_article": cls._article_payload(representative),
            "corroborating_articles": [cls._article_payload(article) for article in corroborating],
        }
        if item.external_event:
            context["approved_impact_links"] = [
                {
                    "company": link.candidate.company,
                    "impact_direction": link.decision.impact_direction,
                    "causal_mechanism": link.decision.causal_mechanism,
                    "materiality": link.decision.materiality,
                    "event_family": link.decision.event_family,
                }
                for link in item.external_event.impact_links
            ]
        elif item.external_match:
            decision = item.external_match.decision
            context["approved_impact_links"] = [{"company": item.external_match.candidate.company, "impact_direction": decision.impact_direction, "causal_mechanism": decision.causal_mechanism, "materiality": decision.materiality, "event_family": decision.event_family}]
        return json.dumps(context, ensure_ascii=False), {article_id(article) for article in evidence}
