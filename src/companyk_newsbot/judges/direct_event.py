"""One-call Route A event assessment and one-call final grounding."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from companyk_newsbot.dedup import EventCluster, article_id


class DirectEventAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["DELIVER", "IGNORE"]
    reason_code: str = Field(min_length=1, max_length=80)
    materiality: Literal["high", "medium", "none"]
    event_family: str = Field(min_length=1, max_length=80)
    fact_summary: str | None = Field(default=None, max_length=500)
    investor_insight: str | None = Field(default=None, max_length=350)
    evidence_article_ids: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def coherent(self) -> "DirectEventAssessment":
        if self.decision == "IGNORE" and (self.materiality != "none" or self.fact_summary or self.investor_insight):
            raise ValueError("IGNORE must have none materiality and null briefing fields")
        if self.decision == "DELIVER" and (self.materiality == "none" or not self.fact_summary or not self.evidence_article_ids):
            raise ValueError("DELIVER requires materiality, fact_summary, and evidence IDs")
        return self


class DirectGroundingVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_summary: Literal["SUPPORTED", "UNSUPPORTED"]
    investor_insight: Literal["SUPPORTED", "UNSUPPORTED", "NOT_PRESENT"]
    unsupported_claims: list[str] = Field(default_factory=list, max_length=8)


@dataclass
class UsageMetrics:
    calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    failures: int = 0
    invalid_evidence_fallbacks: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def payload(self, prefix: str) -> dict[str, object]:
        ordered = sorted(self.latencies_ms)
        p50 = ordered[(len(ordered) - 1) // 2] if ordered else 0
        return {f"{prefix}_calls": self.calls, f"{prefix}_input_tokens": self.input_tokens,
            f"{prefix}_cached_input_tokens": self.cached_input_tokens, f"{prefix}_output_tokens": self.output_tokens,
            f"{prefix}_reasoning_tokens": self.reasoning_tokens, f"{prefix}_failures": self.failures,
            f"{prefix}_invalid_evidence_fallbacks": self.invalid_evidence_fallbacks,
            f"{prefix}_latency_p50_ms": round(p50, 2)}

    def record(self, response: Any, latency_ms: float) -> None:
        self.latencies_ms.append(latency_ms)
        usage = getattr(response, "usage", None)
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.cached_input_tokens += int(getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0)
        self.reasoning_tokens += int(getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0)


ASSESSMENT_PROMPT = """Assess one direct portfolio-company proto-event. DELIVER only a new decision-relevant event; IGNORE passing mentions, interviews, awards, conferences, promotion, stale rehash, or events about another entity. Be selective. Return a short Korean fact sentence. Investor insight may be null and must never be generic or speculative. Use only supplied evidence; return only supplied article IDs. No chain-of-thought."""
GROUNDING_PROMPT = """Verify the proposed Korean fact summary and optional investor insight only against supplied event evidence. Unsupported means any material claim is not directly supported or tightly entailed. Return verdict fields only."""


def event_payload(event: EventCluster) -> dict[str, object]:
    articles = [event.primary.article, *(match.article for match in event.coverage[:3])]
    return {"company": event.company, "matched_identity_terms": sorted({term for match in (event.primary, *event.coverage) for term in match.matched_terms}),
        "event_id": event.event_id, "coverage_count": event.coverage_count, "anchors": event.anchors.payload(),
        "articles": [{"article_id": article_id(a), "title": a.title, "description": a.description, "text": a.text,
            "source": a.source, "published_at": a.published_at.isoformat() if a.published_at else None} for a in articles]}


class DirectEventJudge:
    def __init__(self, client: Any, *, model: str = "gpt-5.6-luna", reasoning_effort: str = "low") -> None:
        if "sol" in model.casefold(): raise ValueError("Direct Event Judge must not use Sol")
        self.client, self.model, self.reasoning_effort = client, model, reasoning_effort
        self.metrics = UsageMetrics()

    @classmethod
    def from_environment(cls) -> "DirectEventJudge":
        from openai import OpenAI
        return cls(OpenAI(timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))), model=os.getenv("DIRECT_EVENT_MODEL", "gpt-5.6-luna"), reasoning_effort=os.getenv("DIRECT_EVENT_REASONING", "low"))

    def assess(self, event: EventCluster) -> DirectEventAssessment:
        started = monotonic(); self.metrics.calls += 1
        try:
            response = self.client.responses.parse(model=self.model, reasoning={"effort": self.reasoning_effort}, text_format=DirectEventAssessment,
                input=[{"role": "system", "content": ASSESSMENT_PROMPT}, {"role": "user", "content": json.dumps(event_payload(event), ensure_ascii=False)}])
            parsed = response.output_parsed
            if not isinstance(parsed, DirectEventAssessment): raise ValueError("missing structured assessment")
            valid_ids = {article_id(match.article) for match in event.all_matches}
            if any(value not in valid_ids for value in parsed.evidence_article_ids):
                # Never repair an LLM-supplied claim with invented support. This
                # event is conservatively excluded while the remaining events
                # continue through the batch.
                self.metrics.record(response, (monotonic() - started) * 1000)
                self.metrics.invalid_evidence_fallbacks += 1
                return DirectEventAssessment(
                    decision="IGNORE",
                    reason_code="invalid_evidence_id",
                    materiality="none",
                    event_family="other",
                    fact_summary=None,
                    investor_insight=None,
                    evidence_article_ids=[],
                )
            self.metrics.record(response, (monotonic() - started) * 1000)
            return parsed
        except Exception:
            self.metrics.failures += 1; self.metrics.latencies_ms.append((monotonic() - started) * 1000); raise


class DirectEventGrounder:
    def __init__(self, client: Any, *, model: str = "gpt-5.6-luna", reasoning_effort: str = "low") -> None:
        if "sol" in model.casefold(): raise ValueError("Direct grounding must not use Sol")
        self.client, self.model, self.reasoning_effort = client, model, reasoning_effort
        self.metrics = UsageMetrics()

    @classmethod
    def from_environment(cls) -> "DirectEventGrounder":
        from openai import OpenAI
        return cls(OpenAI(timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))), model=os.getenv("DIRECT_GROUNDING_MODEL", "gpt-5.6-luna"), reasoning_effort=os.getenv("DIRECT_GROUNDING_REASONING", "low"))

    def ground(self, event: EventCluster, assessment: DirectEventAssessment) -> tuple[str, str | None, DirectGroundingVerdict]:
        valid_ids = {article_id(match.article) for match in event.all_matches}
        if any(value not in valid_ids for value in assessment.evidence_article_ids):
            verdict = DirectGroundingVerdict(fact_summary="UNSUPPORTED", investor_insight="UNSUPPORTED" if assessment.investor_insight else "NOT_PRESENT", unsupported_claims=["unknown evidence id"])
            return event.primary.article.title, None, verdict
        started = monotonic(); self.metrics.calls += 1
        try:
            response = self.client.responses.parse(model=self.model, reasoning={"effort": self.reasoning_effort}, text_format=DirectGroundingVerdict,
                input=[{"role": "system", "content": GROUNDING_PROMPT}, {"role": "user", "content": json.dumps({"event": event_payload(event), "assessment": assessment.model_dump()}, ensure_ascii=False)}])
            verdict = response.output_parsed
            if not isinstance(verdict, DirectGroundingVerdict): raise ValueError("missing grounding verdict")
            self.metrics.record(response, (monotonic() - started) * 1000)
        except Exception:
            self.metrics.failures += 1; self.metrics.latencies_ms.append((monotonic() - started) * 1000); raise
        fact = assessment.fact_summary if verdict.fact_summary == "SUPPORTED" else event.primary.article.title
        insight = assessment.investor_insight if verdict.investor_insight == "SUPPORTED" else None
        return fact or event.primary.article.title, insight, verdict
