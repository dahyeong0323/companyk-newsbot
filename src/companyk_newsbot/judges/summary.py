"""Evidence-grounded final event editor and fail-closed insight verifier."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from companyk_newsbot.dedup.anchors import EventAnchors
from companyk_newsbot.dedup.event import article_id
from companyk_newsbot.dedup.representative import RepresentativeArticleSelector
from companyk_newsbot.models import Article
from companyk_newsbot.ranking import RankedNewsItem

SUMMARY_PROMPT_VERSION = "grounded_event_editor_v3"
SUMMARY_SYSTEM_PROMPT = """Write one concise Korean factual summary and one executive/investment insight for the supplied ranked event.
Use only supplied evidence. Do not introduce valuation, ownership, runway, revenue, market share, probability,
timeline, or causal claims unless directly supported or tightly logically entailed by approved context. If a
defensible implication is not supported, use insight_mode=watchpoint and identify a concrete observable variable
or milestone. Never use generic filler such as 귀추가 주목된다, 긍정적 영향, 부정적 영향, 기업가치 상승 기대,
경쟁력 강화 전망, 성장이 기대된다, or 큰 도움이 될 전망. Never invent a stronger implication merely to
avoid watchpoint mode. Return only unique supplied article IDs."""

GROUNDING_PROMPT_VERSION = "insight_grounding_verifier_v1"
GROUNDING_SYSTEM_PROMPT = """Verify only whether every material claim in the proposed insight is directly supported by,
or tightly logically entailed by, the supplied article evidence and approved company-specific causal context.
A valid evidence ID alone is not sufficient. Reject speculative valuation, competitiveness, growth, runway,
market share, revenue, exit-probability, or other investment-outcome claims unless supplied evidence supports them.
Return a short verdict, not chain-of-thought."""

_BANNED_WATCHPOINT_PHRASES = (
    "귀추가 주목된다", "긍정적 영향", "부정적 영향", "기업가치 상승 기대", "경쟁력 강화 전망",
    "성장이 기대된다", "큰 도움이 될 전망", "지켜볼 필요가 있다", "향후 상황을 지켜",
)


class SummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_summary: str = Field(min_length=1, max_length=500)
    why_it_matters: str | None = Field(default=None, max_length=350)
    insight_one_liner: str = Field(min_length=1, max_length=500)
    insight_dimension: Literal["exit_liquidity", "financing_runway", "valuation_comps", "revenue_traction", "regulatory_clinical", "commercialization", "cost_supply", "customer_platform", "competition", "governance", "strategy", "other"]
    insight_mode: Literal["implication", "watchpoint"]
    confidence: Literal["high", "medium"]
    # Empty/default is intentional: all evidence errors share one application-level retry path.
    evidence_article_ids: list[str] = Field(default_factory=list, max_length=4)

    @property
    def summary(self) -> str:
        return self.fact_summary


class GroundingVerifierOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["SUPPORTED", "UNSUPPORTED"]
    unsupported_claims: list[str] = Field(default_factory=list, max_length=8)
    short_reason: str = Field(min_length=1, max_length=400)


class SummaryError(RuntimeError):
    pass


class ResponsesParser(Protocol):
    def parse(self, **kwargs: Any) -> Any: ...


class SummaryClient(Protocol):
    responses: ResponsesParser


@dataclass
class SummaryMetrics:
    calls: int = 0
    validation_retries: int = 0
    failures: int = 0
    implication_count: int = 0
    watchpoint_count: int = 0
    grounding_calls: int = 0
    grounding_failures: int = 0
    unsupported_implications: int = 0
    watchpoint_rewrites: int = 0

    @property
    def evidence_retries(self) -> int:
        return self.validation_retries

    def payload(self) -> dict[str, int]:
        return {
            "summary_calls": self.calls,
            "summary_retries": self.validation_retries,
            "summary_evidence_retries": self.validation_retries,
            "summary_failures": self.failures,
            "insight_implication_count": self.implication_count,
            "insight_watchpoint_count": self.watchpoint_count,
            "grounding_verifier_calls": self.grounding_calls,
            "grounding_verifier_failures": self.grounding_failures,
            "unsupported_implications": self.unsupported_implications,
            "watchpoint_rewrites": self.watchpoint_rewrites,
        }


class InsightGroundingVerifier:
    def __init__(self, client: SummaryClient, *, model: str = "gpt-5.6-luna", reasoning_effort: str = "medium") -> None:
        if not model.strip():
            raise ValueError("grounding verifier model must not be blank")
        self._client, self.model, self.reasoning_effort = client, model, reasoning_effort

    def verify(self, event_payload: str, proposed: SummaryOutput) -> GroundingVerifierOutput:
        response = self._client.responses.parse(
            model=self.model,
            reasoning={"effort": self.reasoning_effort},
            text_format=GroundingVerifierOutput,
            input=[
                {"role": "system", "content": GROUNDING_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({
                    "event": json.loads(event_payload),
                    "proposed_insight": {
                        "insight_one_liner": proposed.insight_one_liner,
                        "insight_dimension": proposed.insight_dimension,
                        "insight_mode": proposed.insight_mode,
                        "confidence": proposed.confidence,
                        "evidence_article_ids": proposed.evidence_article_ids,
                    },
                }, ensure_ascii=False)},
            ],
        )
        parsed = getattr(response, "output_parsed", None)
        if not isinstance(parsed, GroundingVerifierOutput):
            raise SummaryError("grounding verifier returned malformed structured output")
        return parsed


class NewsSummarizer:
    def __init__(self, client: SummaryClient, *, model: str, reasoning_effort: str = "medium", grounding_verifier: InsightGroundingVerifier | None = None) -> None:
        if not model.strip():
            raise ValueError("OpenAI model must not be blank")
        self._client, self.model, self.reasoning_effort = client, model, reasoning_effort
        self.grounding_verifier = grounding_verifier
        self.metrics = SummaryMetrics()

    def summarize(self, item: RankedNewsItem) -> SummaryOutput:
        payload, valid_ids = self._payload(item)
        parsed = self._validated_editor_result(item, payload, valid_ids)
        parsed = self._verify_or_rewrite(item, payload, valid_ids, parsed)
        if parsed.insight_mode == "implication":
            self.metrics.implication_count += 1
        else:
            self.metrics.watchpoint_count += 1
        return parsed

    def _validated_editor_result(self, item: RankedNewsItem, payload: str, valid_ids: set[str]) -> SummaryOutput:
        corrective = ""
        for attempt in range(2):
            parsed = self._editor_call(payload + corrective)
            validation_error = self._validation_error(item, parsed, valid_ids)
            if validation_error is None:
                return parsed
            if attempt == 0:
                self.metrics.validation_retries += 1
                corrective = (
                    "\nCORRECTION: Return a complete corrected result. evidence_article_ids must be non-empty, unique, "
                    "and contain only supplied IDs. Preserve the route-specific shape and use a concrete watchpoint "
                    f"instead of generic filler. Previous validation error: {validation_error}"
                )
                continue
            self.metrics.failures += 1
            raise SummaryError(f"summary validation failed after one retry: {validation_error}")
        raise AssertionError("unreachable")

    def _editor_call(self, content: str) -> SummaryOutput:
        self.metrics.calls += 1
        try:
            response = self._client.responses.parse(
                model=self.model,
                input=[{"role": "system", "content": SUMMARY_SYSTEM_PROMPT}, {"role": "user", "content": content}],
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
        return parsed

    def _verify_or_rewrite(self, item: RankedNewsItem, payload: str, valid_ids: set[str], proposed: SummaryOutput) -> SummaryOutput:
        if self.grounding_verifier is None:
            self.metrics.failures += 1
            raise SummaryError("final insight requires the Luna grounding verifier")
        verdict = self._grounding_verdict(payload, proposed)
        if verdict.decision == "SUPPORTED":
            return proposed
        if proposed.insight_mode == "watchpoint":
            self.metrics.failures += 1
            raise SummaryError("watchpoint was not supported by supplied evidence")
        self.metrics.unsupported_implications += 1
        self.metrics.watchpoint_rewrites += 1
        rewrite_instruction = (
            "\nREWRITE REQUIRED: The grounding verifier rejected the implication. Rewrite exactly once as "
            "insight_mode=watchpoint. Identify a concrete observable variable or milestone using only supplied "
            "evidence. Do not return another implication and do not use generic filler."
        )
        rewritten = self._editor_call(payload + rewrite_instruction)
        error = self._validation_error(item, rewritten, valid_ids)
        if rewritten.insight_mode != "watchpoint":
            error = "unsupported implication rewrite must return watchpoint"
        if error is not None:
            self.metrics.failures += 1
            raise SummaryError(f"watchpoint rewrite failed validation: {error}")
        rewrite_verdict = self._grounding_verdict(payload, rewritten)
        if rewrite_verdict.decision != "SUPPORTED":
            self.metrics.failures += 1
            raise SummaryError("watchpoint rewrite failed grounding validation")
        return rewritten

    def _grounding_verdict(self, payload: str, proposed: SummaryOutput) -> GroundingVerifierOutput:
        self.metrics.grounding_calls += 1
        try:
            return self.grounding_verifier.verify(payload, proposed)  # type: ignore[union-attr]
        except Exception as exc:
            self.metrics.grounding_failures += 1
            self.metrics.failures += 1
            raise SummaryError("grounding verifier failed closed") from exc

    @staticmethod
    def _validation_error(item: RankedNewsItem, parsed: SummaryOutput, valid_ids: set[str]) -> str | None:
        if item.route == "external" and not parsed.why_it_matters:
            return "external-impact summaries must include why_it_matters"
        if item.route == "direct" and parsed.why_it_matters:
            return "direct-company summaries must not include why_it_matters"
        ids = parsed.evidence_article_ids
        if not ids:
            return "evidence_article_ids is missing or empty"
        if len(ids) != len(set(ids)):
            return "evidence_article_ids contains duplicates"
        if not set(ids).issubset(valid_ids):
            return "evidence_article_ids contains an unknown or unsupplied ID"
        if parsed.insight_mode == "watchpoint":
            normalized = " ".join(parsed.insight_one_liner.casefold().split())
            if any(phrase in normalized for phrase in _BANNED_WATCHPOINT_PHRASES):
                return "watchpoint contains banned generic filler"
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
        event_family_context: dict[str, object] = {}
        if item.direct_event:
            representative = item.direct_event.primary.article
            alternatives = [match.article for match in item.direct_event.coverage]
            anchors = item.direct_event.anchors
        elif item.external_event:
            representative = item.external_event.representative.candidate.article
            alternatives = [article for article in item.external_event.all_articles if article_id(article) != article_id(representative)]
            anchors = item.external_event.anchors
            event_family_context = {"canonical_event_family": item.external_event.event_family, "source_event_families": list(item.external_event.source_families)}
        else:
            representative = item.direct_match.article if item.direct_match else item.external_match.candidate.article
            alternatives = []
            family = item.external_match.decision.event_family if item.external_match else None
            anchors = EventAnchors.from_article(representative, event_families=(family,) if family else ())
            if family:
                event_family_context = {"canonical_event_family": family, "source_event_families": [family]}
        corroborating = selector.corroborating(representative, alternatives, limit=2)
        evidence = (representative, *corroborating)
        context: dict[str, object] = {
            "route": item.route,
            "event_id": item.event_id,
            "company": item.company,
            "impacted_companies": list(item.impacted_companies),
            "event_materiality": item.materiality,
            "canonical_event_anchors": anchors.payload(),
            "event_family_context": event_family_context,
            "representative_article": cls._article_payload(representative),
            "corroborating_articles": [cls._article_payload(article) for article in corroborating],
        }
        if item.external_event:
            context["approved_impact_links"] = [{
                "company": link.candidate.company,
                "impact_direction": link.decision.impact_direction,
                "causal_mechanism": link.decision.causal_mechanism,
                "materiality": link.decision.materiality,
                "event_family": link.decision.event_family,
            } for link in item.external_event.impact_links]
        elif item.external_match:
            decision = item.external_match.decision
            context["approved_impact_links"] = [{
                "company": item.external_match.candidate.company,
                "impact_direction": decision.impact_direction,
                "causal_mechanism": decision.causal_mechanism,
                "materiality": decision.materiality,
                "event_family": decision.event_family,
            }]
        return json.dumps(context, ensure_ascii=False), {article_id(article) for article in evidence}
