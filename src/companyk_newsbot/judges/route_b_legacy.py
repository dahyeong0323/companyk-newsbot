"""Async Luna-primary / Sol-fallback Route B judging with audit metadata."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import os
import random
from time import monotonic
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from companyk_newsbot.judges.route_b import JudgeError, JudgeOutput, JudgedRouteBCandidate, RouteBCausalMaterialityJudge, SYSTEM_PROMPT
from companyk_newsbot.rules.route_b import RouteBCandidate


LUNA_PROMPT_VERSION = "route_b_luna_primary_v1"
SOL_PROMPT_VERSION = "route_b_sol_fallback_v1"
LUNA_SYSTEM_PROMPT = """You are the primary structured judge for one pre-filtered Company K external-impact news candidate.
Apply only the supplied registered company-specific exposure and existing causal-materiality rules. Do not infer new exposures or widen the case into general sector news.

Return ACCEPT only when the article describes a real, material external event that fits the exposure subject, an allowed event family, and has a credible company-specific causal mechanism. Return REJECT for a clear failure, using the supplied existing rejection taxonomy. Broad industry commentary is not enough.

Do not escalate merely because a stronger model is available. If supplied evidence clearly supports ACCEPT or REJECT, make that final decision. Use ESCALATE_TO_SOL only when the evidence is genuinely ambiguous, technically difficult, or insufficient for a reliable decision. Never guess merely to avoid escalation. Do not provide chain-of-thought."""


class LunaJudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["ACCEPT", "REJECT", "ESCALATE_TO_SOL"]
    reason_code: Literal["none", "broad_industry_only", "wrong_context", "non_material", "wrong_jurisdiction", "weak_causal_link", "duplicate_event", "uncertain"]
    short_reason: str = Field(min_length=1, max_length=500)
    confidence: Literal["high", "medium", "low"]
    uncertainty_flags: list[str] = Field(default_factory=list, max_length=8)
    event_family: str = "none"
    materiality: Literal["high", "medium", "low", "none"] = "none"
    impact_direction: Literal["positive", "negative", "mixed", "neutral"] = "neutral"
    causal_mechanism: str = Field(min_length=1, max_length=700)


class AsyncResponsesParser(Protocol):
    def parse(self, **kwargs: Any) -> Any: ...


class AsyncOpenAIResponsesClient(Protocol):
    responses: AsyncResponsesParser


def candidate_id(candidate: RouteBCandidate) -> str:
    raw = "|".join((candidate.company, candidate.exposure_id, candidate.article.canonical_url))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CascadeSettings:
    primary_model: str = "gpt-5.6-luna"
    primary_reasoning: str = "medium"
    fallback_model: str = "gpt-5.6-sol"
    fallback_reasoning: str = "medium"
    luna_concurrency: int = 32
    sol_concurrency: int = 8
    luna_rpm_budget: int = 400
    sol_rpm_budget: int = 120
    luna_timeout_seconds: float = 30.0
    sol_timeout_seconds: float = 60.0
    max_retries: int = 2

    @classmethod
    def from_environment(cls) -> "CascadeSettings":
        def integer(name: str, default: int) -> int:
            try:
                value = int(os.getenv(name, str(default)))
            except ValueError as exc:
                raise JudgeError(f"{name} must be a positive integer") from exc
            if value < 1:
                raise JudgeError(f"{name} must be a positive integer")
            return value
        def number(name: str, default: float) -> float:
            try:
                value = float(os.getenv(name, str(default)))
            except ValueError as exc:
                raise JudgeError(f"{name} must be positive") from exc
            if value <= 0:
                raise JudgeError(f"{name} must be positive")
            return value
        return cls(
            primary_model=os.getenv("ROUTE_B_PRIMARY_MODEL", "gpt-5.6-luna").strip(),
            primary_reasoning=os.getenv("ROUTE_B_PRIMARY_REASONING", "medium").strip(),
            fallback_model=os.getenv("ROUTE_B_FALLBACK_MODEL", "gpt-5.6-sol").strip(),
            fallback_reasoning=os.getenv("ROUTE_B_FALLBACK_REASONING", "medium").strip(),
            luna_concurrency=integer("LUNA_CONCURRENCY", 32), sol_concurrency=integer("SOL_CONCURRENCY", 8),
            luna_rpm_budget=integer("LUNA_RPM_BUDGET", 400), sol_rpm_budget=integer("SOL_RPM_BUDGET", 120),
            luna_timeout_seconds=number("LUNA_REQUEST_TIMEOUT_SECONDS", 30), sol_timeout_seconds=number("SOL_REQUEST_TIMEOUT_SECONDS", 60),
            max_retries=integer("OPENAI_MAX_RETRIES", 2),
        )


class RequestStartLimiter:
    def __init__(self, rpm_budget: int) -> None:
        self.rpm_budget = rpm_budget
        self.starts: list[float] = []
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self.lock:
                now = monotonic()
                self.starts = [started for started in self.starts if now - started < 60]
                if len(self.starts) < self.rpm_budget:
                    self.starts.append(now)
                    return
                wait = max(0.01, 60 - (now - self.starts[0]))
            await asyncio.sleep(wait)


@dataclass
class CascadeMetrics:
    luna_candidates: int = 0; luna_calls: int = 0; luna_accepts: int = 0; luna_rejects: int = 0; luna_escalates: int = 0; luna_error_fallbacks: int = 0
    luna_retries: int = 0; luna_429s: int = 0; luna_timeouts: int = 0; luna_schema_failures: int = 0
    sol_calls: int = 0; sol_accepts: int = 0; sol_rejects: int = 0; sol_unresolved: int = 0; sol_retries: int = 0; sol_429s: int = 0; sol_timeouts: int = 0
    luna_latencies_ms: list[float] = field(default_factory=list); sol_latencies_ms: list[float] = field(default_factory=list)
    luna_usage: Counter[str] = field(default_factory=Counter); sol_usage: Counter[str] = field(default_factory=Counter)
    luna_stage_seconds: float = 0; sol_stage_seconds: float = 0

    @staticmethod
    def _latencies(values: list[float]) -> dict[str, float]:
        if not values: return {"p50": 0, "p95": 0, "max": 0}
        ordered = sorted(values)
        return {"p50": round(ordered[(len(ordered)-1)//2], 2), "p95": round(ordered[min(len(ordered)-1, int(len(ordered)*.95))], 2), "max": round(ordered[-1], 2)}

    def payload(self) -> dict[str, object]:
        luna, sol = self._latencies(self.luna_latencies_ms), self._latencies(self.sol_latencies_ms)
        denom = self.luna_candidates or 1
        return {**{key: getattr(self, key) for key in (
            "luna_candidates", "luna_calls", "luna_accepts", "luna_rejects", "luna_escalates", "luna_error_fallbacks", "luna_retries", "luna_429s", "luna_timeouts", "luna_schema_failures",
            "sol_calls", "sol_accepts", "sol_rejects", "sol_unresolved", "sol_retries", "sol_429s", "sol_timeouts")},
            "luna_stage_wall_clock_seconds": round(self.luna_stage_seconds, 3), "sol_stage_wall_clock_seconds": round(self.sol_stage_seconds, 3),
            "luna_request_latency_p50_ms": luna["p50"], "luna_request_latency_p95_ms": luna["p95"], "luna_request_latency_max_ms": luna["max"],
            "sol_request_latency_p50_ms": sol["p50"], "sol_request_latency_p95_ms": sol["p95"], "sol_request_latency_max_ms": sol["max"],
            "luna_final_decision_rate": round((self.luna_accepts + self.luna_rejects)/denom, 5), "sol_escalation_rate": round(self.sol_calls/denom, 5),
            "luna_input_tokens": self.luna_usage["input_tokens"], "luna_cached_input_tokens": self.luna_usage["cached_input_tokens"], "luna_output_tokens": self.luna_usage["output_tokens"],
            "sol_input_tokens": self.sol_usage["input_tokens"], "sol_cached_input_tokens": self.sol_usage["cached_input_tokens"], "sol_output_tokens": self.sol_usage["output_tokens"]}


class RouteBCascadeJudge:
    def __init__(self, primary_client: AsyncOpenAIResponsesClient, fallback_client: AsyncOpenAIResponsesClient, settings: CascadeSettings) -> None:
        self.primary_client, self.fallback_client, self.settings = primary_client, fallback_client, settings
        self.metrics = CascadeMetrics(); self.luna_semaphore = asyncio.Semaphore(settings.luna_concurrency); self.sol_semaphore = asyncio.Semaphore(settings.sol_concurrency)
        self.luna_limiter, self.sol_limiter = RequestStartLimiter(settings.luna_rpm_budget), RequestStartLimiter(settings.sol_rpm_budget)

    @classmethod
    def from_environment(cls) -> "RouteBCascadeJudge":
        from openai import AsyncOpenAI
        client = AsyncOpenAI(max_retries=0)
        return cls(client, client, CascadeSettings.from_environment())

    def judge_all_sync(self, candidates: tuple[RouteBCandidate, ...] | list[RouteBCandidate]) -> list[JudgedRouteBCandidate]:
        return asyncio.run(self.judge_all(candidates))

    async def judge_all(self, candidates: tuple[RouteBCandidate, ...] | list[RouteBCandidate]) -> list[JudgedRouteBCandidate]:
        self.metrics.luna_candidates = len(candidates)
        started = monotonic(); luna_results = await asyncio.gather(*(self._luna(candidate) for candidate in candidates)); self.metrics.luna_stage_seconds = monotonic()-started
        started = monotonic(); final = await asyncio.gather(*(self._resolve(candidate, luna) for candidate, luna in zip(candidates, luna_results))); self.metrics.sol_stage_seconds = monotonic()-started
        return [result for _, result in sorted(((candidate_id(result.candidate), result) for result in final), key=lambda pair: pair[0])]

    async def _luna(self, candidate: RouteBCandidate) -> tuple[LunaJudgeOutput | None, str | None, dict[str, object]]:
        started = monotonic()
        try:
            output, retries, response = await self._request(self.primary_client, self.settings.primary_model, self.settings.primary_reasoning, LunaJudgeOutput, LUNA_SYSTEM_PROMPT, candidate, self.settings.luna_timeout_seconds, self.luna_semaphore, self.luna_limiter, "luna")
            self._validate_luna(candidate, output); self.metrics.luna_calls += 1; self.metrics.luna_retries += retries; self.metrics.luna_latencies_ms.append((monotonic()-started)*1000); self._usage(self.metrics.luna_usage, response)
            if output.decision == "ACCEPT": self.metrics.luna_accepts += 1
            elif output.decision == "REJECT": self.metrics.luna_rejects += 1
            else: self.metrics.luna_escalates += 1
            return output, None, {"luna_retry_count": retries, "luna_latency_ms": round((monotonic()-started)*1000, 2)}
        except Exception as exc:
            self.metrics.luna_calls += 1; self.metrics.luna_error_fallbacks += 1; self.metrics.luna_latencies_ms.append((monotonic()-started)*1000)
            return None, self._reason(exc, "luna"), {"luna_retry_count": self.settings.max_retries, "luna_error": str(exc), "luna_latency_ms": round((monotonic()-started)*1000, 2)}

    async def _resolve(self, candidate: RouteBCandidate, luna: tuple[LunaJudgeOutput | None, str | None, dict[str, object]]) -> JudgedRouteBCandidate:
        output, error_reason, extra = luna
        if output and output.decision in {"ACCEPT", "REJECT"}:
            decision = JudgeOutput(qualifies=output.decision == "ACCEPT", company=candidate.company, exposure_id=candidate.exposure_id,
                event_family=output.event_family if output.decision == "ACCEPT" else "none", materiality=output.materiality if output.decision == "ACCEPT" else "none",
                impact_direction=output.impact_direction if output.decision == "ACCEPT" else "neutral", causal_mechanism=output.causal_mechanism,
                rejection_reason="none" if output.decision == "ACCEPT" else output.reason_code)
            return self._result(candidate, decision, "luna", extra, output, None, None)
        why = "luna_uncertain" if output else (error_reason or "luna_client_error")
        started = monotonic()
        try:
            decision, retries, response = await self._request(self.fallback_client, self.settings.fallback_model, self.settings.fallback_reasoning, JudgeOutput, SYSTEM_PROMPT, candidate, self.settings.sol_timeout_seconds, self.sol_semaphore, self.sol_limiter, "sol")
            RouteBCausalMaterialityJudge._validate_contract(candidate, decision); self.metrics.sol_calls += 1; self.metrics.sol_retries += retries; self.metrics.sol_latencies_ms.append((monotonic()-started)*1000); self._usage(self.metrics.sol_usage, response)
            if decision.qualifies: self.metrics.sol_accepts += 1
            else: self.metrics.sol_rejects += 1
            extra.update({"sol_retry_count": retries, "sol_latency_ms": round((monotonic()-started)*1000, 2)})
            return self._result(candidate, decision, "sol", extra, output, why, None)
        except Exception as exc:
            self.metrics.sol_calls += 1; self.metrics.sol_unresolved += 1; self.metrics.sol_latencies_ms.append((monotonic()-started)*1000)
            extra["sol_error"] = str(exc)
            unresolved = JudgeOutput(qualifies=False, company=candidate.company, exposure_id=candidate.exposure_id, event_family="none", materiality="none", impact_direction="neutral", causal_mechanism="Terminal Sol technical failure; unresolved, not a business rejection.", rejection_reason="wrong_context")
            return self._result(candidate, unresolved, "unresolved", extra, output, why, self._reason(exc, "sol"))

    async def _request(self, client: AsyncOpenAIResponsesClient, model: str, reasoning: str, schema: type[BaseModel], system_prompt: str, candidate: RouteBCandidate, timeout: float, semaphore: asyncio.Semaphore, limiter: RequestStartLimiter, model_name: str) -> tuple[Any, int, Any]:
        error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                async with semaphore:
                    await limiter.acquire()
                    call = client.responses.parse(model=model, input=[{"role":"system","content":system_prompt},{"role":"user","content":self._payload(candidate)}], text_format=schema, reasoning={"effort":reasoning})
                    response = await asyncio.wait_for(call if inspect.isawaitable(call) else asyncio.to_thread(lambda: call), timeout=timeout)
                parsed = getattr(response, "output_parsed", None)
                if not isinstance(parsed, schema): raise JudgeError(f"{model_name} structured output schema validation failure")
                return parsed, attempt, response
            except Exception as exc:
                error = exc; self._failure_metric(exc, model_name)
                if attempt == self.settings.max_retries or not self._retryable(exc): break
                await asyncio.sleep(min(4, .4*(2**attempt))+random.uniform(0,.15))
        assert error is not None; raise error

    @staticmethod
    def _payload(candidate: RouteBCandidate) -> str:
        article = candidate.article
        return json.dumps({"candidate_id":candidate_id(candidate), "registered_exposure":{"company":candidate.company,"exposure_id":candidate.exposure_id,"subject":candidate.exposure_subject,"allowed_event_families":candidate.allowed_event_families}, "article":{"title":article.title,"source":article.source,"description":article.description,"text":article.text,"url":article.canonical_url,"published_at":article.published_at.isoformat() if article.published_at else None,"query_context":article.origin_metadata.get("query")}}, ensure_ascii=False)

    @staticmethod
    def _validate_luna(candidate: RouteBCandidate, value: LunaJudgeOutput) -> None:
        if value.decision == "ACCEPT" and (value.event_family not in candidate.allowed_event_families or value.materiality == "none"): raise JudgeError("Luna ACCEPT violates registered exposure contract")
        if value.decision == "REJECT" and value.reason_code in {"none", "uncertain"}: raise JudgeError("Luna REJECT requires an existing rejection reason")
        if value.decision == "ESCALATE_TO_SOL" and value.reason_code != "uncertain": raise JudgeError("Luna escalation must use uncertain reason code")

    def _result(self, candidate: RouteBCandidate, decision: JudgeOutput, source: Literal["luna","sol","unresolved"], extra: dict[str,object], luna: LunaJudgeOutput | None, sol_reason: str | None, unresolved: str | None) -> JudgedRouteBCandidate:
        audit = {"candidate_id":candidate_id(candidate), "luna_model":self.settings.primary_model,"luna_reasoning_effort":self.settings.primary_reasoning,"luna_decision":luna.decision if luna else None,"luna_reason_code":luna.reason_code if luna else None,"luna_short_reason":luna.short_reason if luna else None,"luna_confidence":luna.confidence if luna else None,"luna_uncertainty_flags":luna.uncertainty_flags if luna else [],"sol_invoked":source in {"sol","unresolved"},"sol_invocation_reason":sol_reason,"sol_model":self.settings.fallback_model if source in {"sol","unresolved"} else None,"sol_reasoning_effort":self.settings.fallback_reasoning if source in {"sol","unresolved"} else None,"sol_decision":"UNRESOLVED" if source == "unresolved" else ("ACCEPT" if source == "sol" and decision.qualifies else "REJECT" if source == "sol" else None),"final_decision":"UNRESOLVED" if source == "unresolved" else ("ACCEPT" if decision.qualifies else "REJECT"),"final_decision_source":source,"unresolved_reason":unresolved,**extra}
        return JudgedRouteBCandidate(candidate, decision, LUNA_PROMPT_VERSION if source == "luna" else SOL_PROMPT_VERSION, self.settings.primary_model if source == "luna" else self.settings.fallback_model, audit)

    def _failure_metric(self, exc: Exception, model: str) -> None:
        text = str(exc).casefold()
        if "429" in text: setattr(self.metrics, f"{model}_429s", getattr(self.metrics,f"{model}_429s")+1)
        if isinstance(exc, TimeoutError) or "timeout" in text: setattr(self.metrics, f"{model}_timeouts", getattr(self.metrics,f"{model}_timeouts")+1)
        if model == "luna" and ("schema" in text or "malformed" in text): self.metrics.luna_schema_failures += 1
    @staticmethod
    def _retryable(exc: Exception) -> bool:
        text = str(exc).casefold(); return isinstance(exc, TimeoutError) or any(v in text for v in ("429","5xx","server error","connection","timeout"))
    @staticmethod
    def _reason(exc: Exception, model: str) -> str:
        text = str(exc).casefold()
        if isinstance(exc, TimeoutError) or "timeout" in text: return f"{model}_timeout"
        if "429" in text: return f"{model}_429_exhausted"
        if "5xx" in text or "server error" in text: return f"{model}_5xx_exhausted"
        if "schema" in text or "malformed" in text: return f"{model}_schema_failure"
        return f"{model}_client_error"
    @staticmethod
    def _usage(counter: Counter[str], response: Any) -> None:
        usage = getattr(response,"usage",None)
        for field in ("input_tokens","output_tokens"):
            value = getattr(usage,field,None) if usage else None
            if isinstance(value,int): counter[field]+=value
        cached = getattr(getattr(usage,"input_tokens_details",None),"cached_tokens",None) if usage else None
        if isinstance(cached,int): counter["cached_input_tokens"]+=cached
