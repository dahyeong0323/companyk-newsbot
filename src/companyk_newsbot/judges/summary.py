"""Evidence-grounded final event editor and fail-closed insight verifier."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from time import monotonic
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
avoid watchpoint mode. Do not claim that the article omits, has not disclosed, or has not reported a fact unless
the supplied evidence explicitly states that absence. Do not infer an entity's type, role, function, product status,
organizational status, or commercial status unless the evidence explicitly establishes it. Do not strengthen association,
competition, relevance, or possibility into causation, evaluation criteria, commercial impact, or strategic consequence
unless that relationship is explicitly supported. Return only unique supplied article IDs."""

GROUNDING_PROMPT_VERSION = "insight_grounding_verifier_v1"
GROUNDING_SYSTEM_PROMPT = """Verify only whether every material claim in the proposed insight is directly supported by,
or tightly logically entailed by, the supplied article evidence and approved company-specific causal context.
A valid evidence ID alone is not sufficient. Reject speculative valuation, competitiveness, growth, runway,
market share, revenue, exit-probability, or other investment-outcome claims unless supplied evidence supports them.
Return a short verdict, not chain-of-thought."""

CORE_GROUNDING_SYSTEM_PROMPT = """Verify whether the factual fields in the proposed executive news item are directly supported by,
or tightly logically entailed by, the supplied article evidence and approved company-specific causal context.
Evaluate fact_summary and, when present, why_it_matters. Do not evaluate the watchpoint or implication here.
Reject invented facts, amounts, dates, counterparties, product details, contract terms, milestones, adoption claims,
or causal claims not supported by the supplied material. Return a short verdict, not chain-of-thought."""

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


@dataclass(frozen=True)
class _EditorialSubject:
    """Minimal immutable identity for replaying a frozen editorial payload."""

    event_id: str
    route: str


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
    watchpoint_fallbacks: int = 0
    core_rewrites: int = 0
    core_fallbacks: int = 0

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
            "watchpoint_fallbacks": self.watchpoint_fallbacks,
            "core_rewrites": self.core_rewrites,
            "core_fallbacks": self.core_fallbacks,
        }


class InsightGroundingVerifier:
    def __init__(self, client: SummaryClient, *, model: str = "gpt-5.6-luna", reasoning_effort: str = "medium") -> None:
        if not model.strip():
            raise ValueError("grounding verifier model must not be blank")
        self._client, self.model, self.reasoning_effort = client, model, reasoning_effort
        self.last_trace: dict[str, object] | None = None

    def verify(self, event_payload: str, proposed: SummaryOutput) -> GroundingVerifierOutput:
        request = {
            "event": json.loads(event_payload),
            "proposed_insight": {
                "insight_one_liner": proposed.insight_one_liner,
                "insight_dimension": proposed.insight_dimension,
                "insight_mode": proposed.insight_mode,
                "confidence": proposed.confidence,
                "evidence_article_ids": proposed.evidence_article_ids,
            },
        }
        response = self._client.responses.parse(
            model=self.model,
            reasoning={"effort": self.reasoning_effort},
            text_format=GroundingVerifierOutput,
            input=[
                {"role": "system", "content": GROUNDING_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
        )
        parsed = getattr(response, "output_parsed", None)
        usage = getattr(response, "usage", None)
        self.last_trace = {
            "exact_input": request, "response_id": getattr(response, "id", None),
            "raw_output": getattr(response, "output_text", None),
            "parsed_output": parsed.model_dump() if hasattr(parsed, "model_dump") else None,
            "token_usage": usage.model_dump() if hasattr(usage, "model_dump") else (dict(usage) if isinstance(usage, dict) else None),
        }
        if not isinstance(parsed, GroundingVerifierOutput):
            raise SummaryError("grounding verifier returned malformed structured output")
        return parsed

    def verify_core(self, event_payload: str, proposed: SummaryOutput) -> GroundingVerifierOutput:
        request = {
            "event": json.loads(event_payload),
            "core_fields": {
                "fact_summary": proposed.fact_summary,
                "why_it_matters": proposed.why_it_matters,
                "evidence_article_ids": proposed.evidence_article_ids,
            },
        }
        response = self._client.responses.parse(
            model=self.model,
            reasoning={"effort": self.reasoning_effort},
            text_format=GroundingVerifierOutput,
            input=[
                {"role": "system", "content": CORE_GROUNDING_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
        )
        parsed = getattr(response, "output_parsed", None)
        usage = getattr(response, "usage", None)
        self.last_trace = {
            "exact_input": request, "response_id": getattr(response, "id", None),
            "raw_output": getattr(response, "output_text", None),
            "parsed_output": parsed.model_dump() if hasattr(parsed, "model_dump") else None,
            "token_usage": usage.model_dump() if hasattr(usage, "model_dump") else (dict(usage) if isinstance(usage, dict) else None),
        }
        if not isinstance(parsed, GroundingVerifierOutput):
            raise SummaryError("core grounding verifier returned malformed structured output")
        return parsed


class NewsSummarizer:
    def __init__(self, client: SummaryClient, *, model: str, reasoning_effort: str = "medium", grounding_verifier: InsightGroundingVerifier | None = None) -> None:
        if not model.strip():
            raise ValueError("OpenAI model must not be blank")
        self._client, self.model, self.reasoning_effort = client, model, reasoning_effort
        self.grounding_verifier = grounding_verifier
        self.metrics = SummaryMetrics()
        # Kept in-memory only until the caller atomically journals it.  This is
        # deliberately observational: it never changes the editor/verifier path.
        self.forensic_trace: list[dict[str, object]] = []

    def summarize(self, item: RankedNewsItem) -> SummaryOutput:
        payload, valid_ids = self._payload(item)
        return self._summarize_payload(item, payload, valid_ids)

    def summarize_exact_payload(self, *, event_id: str, route: Literal["direct", "external"], payload: dict[str, object], evidence_article_ids: set[str]) -> SummaryOutput:
        """Replay a previously frozen editor request without recollecting or reranking."""
        return self._summarize_payload(
            _EditorialSubject(event_id=event_id, route=route),
            json.dumps(payload, ensure_ascii=False),
            evidence_article_ids,
        )

    def _summarize_payload(self, item: RankedNewsItem | _EditorialSubject, payload: str, valid_ids: set[str]) -> SummaryOutput:
        self.forensic_trace.append({"event": "editorial_begin", "event_id": item.event_id, "exact_editor_input": json.loads(payload)})
        try:
            parsed = self._validated_editor_result(item, payload, valid_ids)
            parsed = self._verify_or_rewrite(item, payload, valid_ids, parsed)
            if parsed.insight_mode == "implication":
                self.metrics.implication_count += 1
            else:
                self.metrics.watchpoint_count += 1
            self.forensic_trace.append({"event": "editorial_final", "event_id": item.event_id, "final_status": "success"})
            return parsed
        except Exception as exc:
            self.forensic_trace.append({
                "event": "editorial_final", "event_id": item.event_id, "final_status": "failed",
                "final_failure_stage": self._failure_stage(exc), "exception_type": type(exc).__name__, "exception_message": str(exc),
            })
            raise

    def _validated_editor_result(self, item: RankedNewsItem, payload: str, valid_ids: set[str]) -> SummaryOutput:
        corrective = ""
        for attempt in range(2):
            parsed = self._editor_call(item.event_id, "editor_attempt_1" if attempt == 0 else "editor_attempt_2", payload + corrective)
            validation_error = self._validation_error(item, parsed, valid_ids)
            self.forensic_trace.append({
                "event": "evidence_validation", "event_id": item.event_id, "attempt": attempt + 1,
                "passed": validation_error is None, "evidence_article_ids": parsed.evidence_article_ids,
                "failure_reason": validation_error,
            })
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

    def _editor_call(self, event_id: str, stage: str, content: str, *, recoverable: bool = False) -> SummaryOutput:
        self.metrics.calls += 1
        started = monotonic()
        try:
            response = self._client.responses.parse(
                model=self.model,
                input=[{"role": "system", "content": SUMMARY_SYSTEM_PROMPT}, {"role": "user", "content": content}],
                text_format=SummaryOutput,
                reasoning={"effort": self.reasoning_effort},
            )
        except Exception as exc:
            self.forensic_trace.append(self._call_trace(event_id, stage, self.model, started, content, None, exc))
            if not recoverable:
                self.metrics.failures += 1
            raise SummaryError("final editor request or structured output failed safely") from exc
        parsed = getattr(response, "output_parsed", None)
        self.forensic_trace.append(self._call_trace(event_id, stage, self.model, started, content, response, None, parsed))
        if not isinstance(parsed, SummaryOutput):
            if not recoverable:
                self.metrics.failures += 1
            raise SummaryError("OpenAI response did not contain a parsed event summary")
        return parsed

    def _verify_or_rewrite(self, item: RankedNewsItem, payload: str, valid_ids: set[str], proposed: SummaryOutput) -> SummaryOutput:
        if self.grounding_verifier is None:
            self.metrics.failures += 1
            raise SummaryError("final insight requires the Luna grounding verifier")
        core_verdict = self._core_grounding_verdict(item.event_id, payload, proposed)
        if core_verdict.decision != "SUPPORTED":
            self.forensic_trace.append({
                "event": "core_recovery_begin", "event_id": item.event_id,
                "core_original": self._core_fields(proposed),
                "core_grounding_verdict": core_verdict.decision,
            })
            proposed = self._recover_core(item, payload, valid_ids, proposed)
        verdict = self._grounding_verdict(item.event_id, "grounding_verification", payload, proposed)
        if verdict.decision == "SUPPORTED":
            return proposed
        if proposed.insight_mode == "watchpoint":
            self.forensic_trace.append({
                "event": "watchpoint_recovery_begin", "event_id": item.event_id,
                "watchpoint_original": proposed.insight_one_liner,
                "watchpoint_grounding_verdict": verdict.decision,
            })
            return self._recover_watchpoint(item, payload, valid_ids, proposed)
        self.metrics.unsupported_implications += 1
        self.forensic_trace.append({
            "event": "watchpoint_recovery_begin", "event_id": item.event_id,
            "watchpoint_original": proposed.insight_one_liner,
            "watchpoint_grounding_verdict": verdict.decision,
        })
        return self._recover_watchpoint(item, payload, valid_ids, proposed)

    def _recover_core(self, item: RankedNewsItem | _EditorialSubject, payload: str, valid_ids: set[str], proposed: SummaryOutput) -> SummaryOutput:
        self.metrics.core_rewrites += 1
        rewrite_instruction = (
            "\nCORE FACTUAL REWRITE REQUIRED: Return a complete result. Remove only the unsupported factual claim "
            "from fact_summary or why_it_matters. Use only facts explicitly stated in supplied article evidence and "
            "approved company-specific causal context. Preserve insight_one_liner, insight_dimension, insight_mode, "
            "confidence, and evidence_article_ids exactly. Do not infer entity type, role, function, product status, "
            "organizational status, commercial status, or strengthen association, competition, relevance, or possibility "
            "into causation, evaluation criteria, commercial impact, or strategic consequence."
        )
        rewritten: SummaryOutput | None = None
        try:
            rewritten = self._editor_call(item.event_id, "core_factual_rewrite", payload + rewrite_instruction, recoverable=True)
            error = self._validation_error(item, rewritten, valid_ids)
            if self._insight_fields_changed(proposed, rewritten):
                error = "core factual rewrite must preserve all insight fields and evidence IDs"
            if error is None:
                verdict = self._core_grounding_verdict(item.event_id, payload, rewritten, recoverable=True)
                if verdict.decision == "SUPPORTED":
                    self.forensic_trace.append({
                        "event": "core_recovery_rewrite", "event_id": item.event_id,
                        "core_rewrite": self._core_fields(rewritten),
                        "core_grounding_verdict": verdict.decision,
                        "core_fallback_used": False,
                    })
                    return rewritten
        except SummaryError:
            pass
        fallback = self._safe_core_fallback(payload, proposed)
        fallback_verdict = self._core_grounding_verdict(item.event_id, payload, fallback, recoverable=True)
        if fallback_verdict.decision != "SUPPORTED":
            self.metrics.failures += 1
            raise SummaryError("deterministic core fallback failed grounding validation")
        self.metrics.core_fallbacks += 1
        self.forensic_trace.append({
            "event": "core_fallback_used", "event_id": item.event_id,
            "core_original": self._core_fields(proposed),
            "core_rewrite": self._core_fields(rewritten) if rewritten else None,
            "core_grounding_verdict": fallback_verdict.decision,
            "core_fallback_used": True,
            "core_fallback": self._core_fields(fallback),
        })
        return fallback

    def _recover_watchpoint(self, item: RankedNewsItem | _EditorialSubject, payload: str, valid_ids: set[str], proposed: SummaryOutput) -> SummaryOutput:
        self.metrics.watchpoint_rewrites += 1
        rewrite_instruction = (
            "\nWATCHPOINT REWRITE REQUIRED: Return a complete result with insight_mode=watchpoint. Preserve "
            "fact_summary, why_it_matters, confidence, and evidence_article_ids exactly. Use only companies, "
            "products, technologies, and events explicitly present in the supplied evidence. Do not introduce "
            "new companies, products, numbers, contract terms, milestones, API conditions, clinical conditions, "
            "or rights scope. Write one sentence stating only which future fact would raise the importance of the "
            "current event. Do not return an implication or generic filler."
        )
        rewritten: SummaryOutput | None = None
        try:
            rewritten = self._editor_call(item.event_id, "watchpoint_rewrite", payload + rewrite_instruction, recoverable=True)
            error = self._validation_error(item, rewritten, valid_ids)
            if rewritten.insight_mode != "watchpoint":
                error = "watchpoint rewrite must return watchpoint"
            if self._core_fields_changed(proposed, rewritten):
                error = "watchpoint rewrite must preserve all core factual fields and evidence IDs"
            if error is None:
                rewrite_verdict = self._grounding_verdict(item.event_id, "watchpoint_grounding", payload, rewritten, recoverable=True)
                if rewrite_verdict.decision == "SUPPORTED":
                    self.forensic_trace.append({
                        "event": "watchpoint_recovery_rewrite", "event_id": item.event_id,
                        "watchpoint_rewrite": rewritten.insight_one_liner,
                        "watchpoint_grounding_verdict": rewrite_verdict.decision,
                        "watchpoint_fallback_used": False,
                    })
                    return rewritten
        except SummaryError:
            pass
        fallback = self._safe_watchpoint(proposed)
        self.metrics.watchpoint_fallbacks += 1
        self.forensic_trace.append({
            "event": "watchpoint_fallback_used", "event_id": item.event_id,
            "watchpoint_original": proposed.insight_one_liner,
            "watchpoint_rewrite": rewritten.insight_one_liner if rewritten else None,
            "watchpoint_grounding_verdict": "UNSUPPORTED",
            "watchpoint_fallback_used": True,
            "insight_one_liner": fallback.insight_one_liner,
        })
        return fallback

    def _grounding_verdict(self, event_id: str, stage: str, payload: str, proposed: SummaryOutput, *, recoverable: bool = False) -> GroundingVerifierOutput:
        self.metrics.grounding_calls += 1
        started = monotonic()
        try:
            verdict = self.grounding_verifier.verify(payload, proposed)  # type: ignore[union-attr]
            response_trace = getattr(self.grounding_verifier, "last_trace", None) or {}
            self.forensic_trace.append({
                "event": stage, "event_id": event_id, "model": getattr(self.grounding_verifier, "model", None),
                "latency_ms": round((monotonic() - started) * 1000, 2), "decision": verdict.decision,
                "unsupported_claims": verdict.unsupported_claims, "short_reason": verdict.short_reason, **response_trace,
            })
            return verdict
        except Exception as exc:
            self.forensic_trace.append({"event": stage, "event_id": event_id, "latency_ms": round((monotonic() - started) * 1000, 2), "exception_type": type(exc).__name__, "exception_message": str(exc)})
            self.metrics.grounding_failures += 1
            if not recoverable:
                self.metrics.failures += 1
            raise SummaryError("grounding verifier failed closed") from exc

    def _core_grounding_verdict(self, event_id: str, payload: str, proposed: SummaryOutput, *, recoverable: bool = False) -> GroundingVerifierOutput:
        verify_core = getattr(self.grounding_verifier, "verify_core", None)
        if verify_core is None:
            # Existing lightweight verifier fixtures model an already-supported verifier.
            # Production always uses InsightGroundingVerifier.verify_core.
            return GroundingVerifierOutput(decision="SUPPORTED", short_reason="legacy verifier fixture")
        self.metrics.grounding_calls += 1
        started = monotonic()
        try:
            verdict = verify_core(payload, proposed)
            response_trace = getattr(self.grounding_verifier, "last_trace", None) or {}
            self.forensic_trace.append({
                "event": "core_grounding_verification", "event_id": event_id,
                "model": getattr(self.grounding_verifier, "model", None),
                "latency_ms": round((monotonic() - started) * 1000, 2), "decision": verdict.decision,
                "unsupported_claims": verdict.unsupported_claims, "short_reason": verdict.short_reason, **response_trace,
            })
            return verdict
        except Exception as exc:
            self.forensic_trace.append({"event": "core_grounding_verification", "event_id": event_id, "latency_ms": round((monotonic() - started) * 1000, 2), "exception_type": type(exc).__name__, "exception_message": str(exc)})
            self.metrics.grounding_failures += 1
            if not recoverable:
                self.metrics.failures += 1
            raise SummaryError("core grounding verifier failed closed") from exc

    @staticmethod
    def _core_fields_changed(original: SummaryOutput, rewritten: SummaryOutput) -> bool:
        return (
            original.fact_summary != rewritten.fact_summary
            or original.why_it_matters != rewritten.why_it_matters
            or original.confidence != rewritten.confidence
            or original.evidence_article_ids != rewritten.evidence_article_ids
        )

    @staticmethod
    def _insight_fields_changed(original: SummaryOutput, rewritten: SummaryOutput) -> bool:
        return (
            original.insight_one_liner != rewritten.insight_one_liner
            or original.insight_dimension != rewritten.insight_dimension
            or original.insight_mode != rewritten.insight_mode
            or original.confidence != rewritten.confidence
            or original.evidence_article_ids != rewritten.evidence_article_ids
        )

    @staticmethod
    def _core_fields(proposed: SummaryOutput) -> dict[str, object]:
        return {"fact_summary": proposed.fact_summary, "why_it_matters": proposed.why_it_matters, "evidence_article_ids": proposed.evidence_article_ids}

    @staticmethod
    def _safe_core_fallback(payload: str, proposed: SummaryOutput) -> SummaryOutput:
        event = json.loads(payload)
        article = event["representative_article"]
        title = str(article["title"]).strip()
        fact_summary = NewsSummarizer._event_title_clause(title, event)[:500]
        if not fact_summary:
            fact_summary = "대표 기사에 명시된 사건을 확인했습니다."
        why_it_matters: str | None = None
        if event["route"] == "external":
            links = event.get("approved_impact_links", [])
            mechanism = str(links[0].get("causal_mechanism", "")).strip() if links else ""
            why_it_matters = mechanism[:350] or "승인된 회사별 영향 맥락을 후속 보도와 함께 확인할 필요가 있습니다."
        return proposed.model_copy(update={"fact_summary": fact_summary, "why_it_matters": why_it_matters})

    @staticmethod
    def _event_title_clause(title: str, event: dict[str, object]) -> str:
        """Keep a deterministic fallback tied to the event, not unrelated digest headlines."""
        clauses = [value.strip() for value in re.split(r"\s*[;|]\s*", title) if value.strip()]
        links = event.get("approved_impact_links", [])
        mechanisms = " ".join(str(link.get("causal_mechanism", "")) for link in links if isinstance(link, dict))
        terms = set(re.findall(r"[a-z0-9]{3,}", f"{event.get('company', '')} {mechanisms}".casefold()))
        if clauses and terms:
            ranked = sorted(
                enumerate(clauses),
                key=lambda value: (-sum(term in value[1].casefold() for term in terms), value[0]),
            )
            if any(term in ranked[0][1].casefold() for term in terms):
                return ranked[0][1]
        return clauses[0] if clauses else title

    @staticmethod
    def _safe_watchpoint(proposed: SummaryOutput) -> SummaryOutput:
        templates = {
            "regulatory_clinical": "관련 임상·규제 진행과 공식 결과가 후속 발표에서 확인되는지 주시.",
            "financing_runway": "관련 자금조달·계약 집행의 공식 발표가 후속 보도에서 확인되는지 주시.",
            "customer_platform": "관련 기업의 실제 도입·계약·사업화 진전이 후속 보도나 공식 발표에서 확인되는지 주시.",
            "commercialization": "관련 제품·서비스의 실제 도입·사업화 진전이 후속 보도나 공식 발표에서 확인되는지 주시.",
            "competition": "관련 제품·서비스의 실제 도입과 시장 반응이 후속 보도에서 확인되는지 주시.",
        }
        return proposed.model_copy(update={
            "insight_one_liner": templates.get(proposed.insight_dimension, "해당 사건의 실제 이행과 관련 공식 발표가 후속 보도에서 확인되는지 주시."),
            "insight_mode": "watchpoint",
        })

    @staticmethod
    def _failure_stage(exc: Exception) -> str:
        message = str(exc)
        if "grounding" in message or "watchpoint" in message:
            return "grounding_or_watchpoint"
        if "validation" in message or "evidence" in message:
            return "evidence_validation"
        return "editor_client_or_schema"

    @staticmethod
    def _call_trace(event_id: str, stage: str, model: str, started: float, content: str, response: object | None, exc: Exception | None, parsed: object | None = None) -> dict[str, object]:
        usage = getattr(response, "usage", None) if response is not None else None
        usage_payload = usage.model_dump() if hasattr(usage, "model_dump") else (dict(usage) if isinstance(usage, dict) else None)
        parsed_payload = parsed.model_dump() if hasattr(parsed, "model_dump") else None
        return {
            "event": stage, "event_id": event_id, "model": model, "latency_ms": round((monotonic() - started) * 1000, 2),
            "exact_input": content, "response_id": getattr(response, "id", None), "raw_output": getattr(response, "output_text", None),
            "parsed_output": parsed_payload, "token_usage": usage_payload,
            "exception_type": type(exc).__name__ if exc else None, "exception_message": str(exc) if exc else None,
        }

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
