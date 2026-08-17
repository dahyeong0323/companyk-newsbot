"""Cost-first nano-primary / Luna-escalation Route B judging."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import os
import random
import re
from time import monotonic
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from companyk_newsbot.judges.route_b import JudgeError, JudgeOutput, JudgedRouteBCandidate
from companyk_newsbot.rules.route_b import RouteBCandidate


NANO_PROMPT_VERSION = "route_b_nano_primary_v1"
LUNA_PROMPT_VERSION = "route_b_luna_escalation_v1"

_REJECTION_REASONS = {
    "BROAD_INDUSTRY_ONLY": "broad_industry_only",
    "WRONG_CONTEXT": "wrong_context",
    "NON_MATERIAL": "non_material",
    "WRONG_JURISDICTION": "wrong_jurisdiction",
    "WEAK_CAUSAL_LINK": "weak_causal_link",
    "DUPLICATE_EVENT": "duplicate_event",
    "NO_MATERIAL_LINK": "weak_causal_link",
}
_ACCEPT_REASON = re.compile(
    r"^MATERIAL_LINK\|(?P<family>[a-z0-9_]+)\|(?P<materiality>high|medium|low)\|(?P<direction>positive|negative|mixed|neutral)$"
)

CLASSIFIER_SYSTEM_PROMPT = """You classify one pre-filtered Company K external-impact news candidate under frozen business rules.
Use only the registered company exposure, allowed event families, and supplied article. Do not invent exposures or broaden this into generic sector news.

Return exactly two structured fields: decision and reason_code. Never provide prose or chain-of-thought.

ACCEPT only when entity identity, a real material event, an allowed event family, and a company-specific causal relationship are clear. For ACCEPT use:
MATERIAL_LINK|<allowed_event_family>|<high|medium|low>|<positive|negative|mixed|neutral>

REJECT only when irrelevance is clear. Use one of:
BROAD_INDUSTRY_ONLY, WRONG_CONTEXT, NON_MATERIAL, WRONG_JURISDICTION, WEAK_CAUSAL_LINK, DUPLICATE_EVENT, NO_MATERIAL_LINK

ESCALATE_TO_LUNA only when entity identity, causality, materiality, competitive impact, regulatory impact, clinical impact, or business relevance is genuinely ambiguous. Use AMBIGUOUS. Do not escalate merely because the article is technical or unfamiliar."""

LUNA_SYSTEM_PROMPT = CLASSIFIER_SYSTEM_PROMPT.replace(
    "ESCALATE_TO_LUNA only when entity identity, causality, materiality, competitive impact, regulatory impact, clinical impact, or business relevance is genuinely ambiguous. Use AMBIGUOUS. Do not escalate merely because the article is technical or unfamiliar.",
    "You are the final difficult-case classifier. Return only ACCEPT or REJECT; never escalate. Preserve recall when the supplied evidence supports a plausible material company-specific link.",
)


class NanoJudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["ACCEPT", "REJECT", "ESCALATE_TO_LUNA"]
    reason_code: str = Field(min_length=1, max_length=160)


class LunaJudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["ACCEPT", "REJECT"]
    reason_code: str = Field(min_length=1, max_length=160)


class AsyncResponsesParser(Protocol):
    def parse(self, **kwargs: Any) -> Any: ...


class AsyncOpenAIResponsesClient(Protocol):
    responses: AsyncResponsesParser


def candidate_id(candidate: RouteBCandidate) -> str:
    raw = "|".join((candidate.company, candidate.exposure_id, candidate.article.canonical_url))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CascadeSettings:
    primary_model: str = "gpt-5.4-nano"
    primary_reasoning: str = "low"
    fallback_model: str = "gpt-5.6-luna"
    fallback_reasoning: str = "low"
    nano_concurrency: int = 48
    luna_concurrency: int = 16
    nano_rpm_budget: int = 600
    luna_rpm_budget: int = 300
    nano_timeout_seconds: float = 20.0
    luna_timeout_seconds: float = 30.0
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

        settings = cls(
            primary_model=os.getenv("ROUTE_B_NANO_MODEL", "gpt-5.4-nano").strip(),
            primary_reasoning=os.getenv("ROUTE_B_NANO_REASONING", "low").strip(),
            fallback_model=os.getenv("ROUTE_B_LUNA_MODEL", "gpt-5.6-luna").strip(),
            fallback_reasoning=os.getenv("ROUTE_B_LUNA_REASONING", "low").strip(),
            nano_concurrency=integer("NANO_CONCURRENCY", 48),
            luna_concurrency=integer("LUNA_ESCALATION_CONCURRENCY", 16),
            nano_rpm_budget=integer("NANO_RPM_BUDGET", 600),
            luna_rpm_budget=integer("LUNA_ESCALATION_RPM_BUDGET", 300),
            nano_timeout_seconds=number("NANO_REQUEST_TIMEOUT_SECONDS", 20),
            luna_timeout_seconds=number("LUNA_ESCALATION_TIMEOUT_SECONDS", 30),
            max_retries=integer("OPENAI_MAX_RETRIES", 2),
        )
        if "sol" in settings.primary_model.casefold() or "sol" in settings.fallback_model.casefold():
            raise JudgeError("cost-first Route B models must not use Sol")
        return settings


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
class StageMetrics:
    requests: int = 0
    accepts: int = 0
    rejects: int = 0
    escalates: int = 0
    operational_failures: int = 0
    retries: int = 0
    rate_limits: int = 0
    timeouts: int = 0
    server_errors: int = 0
    schema_failures: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    usage: Counter[str] = field(default_factory=Counter)
    wall_clock_seconds: float = 0.0


@dataclass
class CascadeMetrics:
    candidates: int = 0
    accepted_due_to_classifier_failure: int = 0
    nano: StageMetrics = field(default_factory=StageMetrics)
    luna: StageMetrics = field(default_factory=StageMetrics)

    @staticmethod
    def _latencies(values: list[float]) -> dict[str, float]:
        if not values:
            return {"p50": 0, "p95": 0, "max": 0}
        ordered = sorted(values)
        return {
            "p50": round(ordered[(len(ordered) - 1) // 2], 2),
            "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * .95))], 2),
            "max": round(ordered[-1], 2),
        }

    def payload(self) -> dict[str, object]:
        denom = self.candidates or 1
        payload: dict[str, object] = {
            "route_b_candidates": self.candidates,
            "nano_resolution_rate": round((self.nano.accepts + self.nano.rejects) / denom, 5),
            "luna_escalation_rate": round(self.luna.requests / denom, 5),
            "accepted_due_to_classifier_failure": self.accepted_due_to_classifier_failure,
            "production_sol_calls": 0,
        }
        for name, stage in (("nano", self.nano), ("luna", self.luna)):
            latency = self._latencies(stage.latencies_ms)
            payload.update({
                f"{name}_requests": stage.requests,
                f"{name}_accepts": stage.accepts,
                f"{name}_rejects": stage.rejects,
                f"{name}_escalates": stage.escalates,
                f"{name}_operational_failures": stage.operational_failures,
                f"{name}_retries": stage.retries,
                f"{name}_429s": stage.rate_limits,
                f"{name}_timeouts": stage.timeouts,
                f"{name}_5xxs": stage.server_errors,
                f"{name}_schema_failures": stage.schema_failures,
                f"{name}_stage_wall_clock_seconds": round(stage.wall_clock_seconds, 3),
                f"{name}_request_latency_p50_ms": latency["p50"],
                f"{name}_request_latency_p95_ms": latency["p95"],
                f"{name}_request_latency_max_ms": latency["max"],
                f"{name}_input_tokens": stage.usage["input_tokens"],
                f"{name}_cached_input_tokens": stage.usage["cached_input_tokens"],
                f"{name}_output_tokens": stage.usage["output_tokens"],
                f"{name}_reasoning_tokens": stage.usage["reasoning_tokens"],
            })
        return payload


class RouteBCascadeJudge:
    def __init__(self, primary_client: AsyncOpenAIResponsesClient, fallback_client: AsyncOpenAIResponsesClient, settings: CascadeSettings) -> None:
        self.primary_client = primary_client
        self.fallback_client = fallback_client
        self.settings = settings
        self.metrics = CascadeMetrics()
        self.nano_semaphore = asyncio.Semaphore(settings.nano_concurrency)
        self.luna_semaphore = asyncio.Semaphore(settings.luna_concurrency)
        self.nano_limiter = RequestStartLimiter(settings.nano_rpm_budget)
        self.luna_limiter = RequestStartLimiter(settings.luna_rpm_budget)

    @classmethod
    def from_environment(cls) -> "RouteBCascadeJudge":
        from openai import AsyncOpenAI

        client = AsyncOpenAI(max_retries=0)
        return cls(client, client, CascadeSettings.from_environment())

    def judge_all_sync(self, candidates: tuple[RouteBCandidate, ...] | list[RouteBCandidate]) -> list[JudgedRouteBCandidate]:
        return asyncio.run(self.judge_all(candidates))

    async def judge_all(self, candidates: tuple[RouteBCandidate, ...] | list[RouteBCandidate]) -> list[JudgedRouteBCandidate]:
        self.metrics.candidates = len(candidates)
        started = monotonic()
        nano_results = await asyncio.gather(*(self._nano(candidate) for candidate in candidates))
        self.metrics.nano.wall_clock_seconds = monotonic() - started
        started = monotonic()
        final = await asyncio.gather(*(self._resolve(candidate, nano) for candidate, nano in zip(candidates, nano_results)))
        self.metrics.luna.wall_clock_seconds = monotonic() - started
        return [result for _, result in sorted(((candidate_id(result.candidate), result) for result in final), key=lambda pair: pair[0])]

    async def _nano(self, candidate: RouteBCandidate) -> tuple[NanoJudgeOutput | None, str | None, dict[str, object]]:
        started = monotonic()
        stage = self.metrics.nano
        try:
            output, retries, response = await self._request(
                self.primary_client, self.settings.primary_model, self.settings.primary_reasoning,
                NanoJudgeOutput, CLASSIFIER_SYSTEM_PROMPT, candidate,
                self.settings.nano_timeout_seconds, self.nano_semaphore, self.nano_limiter, "nano",
            )
            self._validate_nano(candidate, output)
            stage.requests += 1
            stage.retries += retries
            self._usage(stage.usage, response)
            if output.decision == "ACCEPT":
                stage.accepts += 1
            elif output.decision == "REJECT":
                stage.rejects += 1
            else:
                stage.escalates += 1
            return output, None, {"nano_retry_count": retries}
        except Exception as exc:
            stage.requests += 1
            stage.operational_failures += 1
            return None, self._reason(exc, "nano"), {"nano_error": str(exc), "nano_retry_count": self.settings.max_retries}
        finally:
            elapsed = (monotonic() - started) * 1000
            stage.latencies_ms.append(elapsed)

    async def _resolve(self, candidate: RouteBCandidate, nano_result: tuple[NanoJudgeOutput | None, str | None, dict[str, object]]) -> JudgedRouteBCandidate:
        output, error_reason, extra = nano_result
        if output and output.decision in {"ACCEPT", "REJECT"}:
            decision = self._decision(candidate, output.decision, output.reason_code)
            return self._result(candidate, decision, "nano", extra, output, None, None, False)

        invocation_reason = "nano_ambiguous" if output else (error_reason or "nano_client_error")
        started = monotonic()
        stage = self.metrics.luna
        try:
            luna, retries, response = await self._request(
                self.fallback_client, self.settings.fallback_model, self.settings.fallback_reasoning,
                LunaJudgeOutput, LUNA_SYSTEM_PROMPT, candidate,
                self.settings.luna_timeout_seconds, self.luna_semaphore, self.luna_limiter, "luna",
            )
            self._validate_luna(candidate, luna)
            stage.requests += 1
            stage.retries += retries
            self._usage(stage.usage, response)
            decision = self._decision(candidate, luna.decision, luna.reason_code)
            if luna.decision == "ACCEPT":
                stage.accepts += 1
            else:
                stage.rejects += 1
            extra["luna_retry_count"] = retries
            return self._result(candidate, decision, "luna", extra, output, luna, invocation_reason, False)
        except Exception as exc:
            stage.requests += 1
            stage.operational_failures += 1
            self.metrics.accepted_due_to_classifier_failure += 1
            failure = self._reason(exc, "luna")
            extra.update({"luna_error": str(exc), "luna_failure": failure})
            decision = self._conservative_accept(candidate)
            return self._result(candidate, decision, "luna_failure_accept", extra, output, None, invocation_reason, True)
        finally:
            stage.latencies_ms.append((monotonic() - started) * 1000)

    async def _request(
        self, client: AsyncOpenAIResponsesClient, model: str, reasoning: str,
        schema: type[BaseModel], system_prompt: str, candidate: RouteBCandidate,
        timeout: float, semaphore: asyncio.Semaphore, limiter: RequestStartLimiter, stage_name: str,
    ) -> tuple[Any, int, Any]:
        error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                async with semaphore:
                    await limiter.acquire()
                    call = client.responses.parse(
                        model=model,
                        input=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": self._payload(candidate)},
                        ],
                        text_format=schema,
                        reasoning={"effort": reasoning},
                    )
                    response = await asyncio.wait_for(
                        call if inspect.isawaitable(call) else asyncio.to_thread(lambda: call), timeout=timeout
                    )
                parsed = getattr(response, "output_parsed", None)
                if not isinstance(parsed, schema):
                    raise JudgeError(f"{stage_name} structured output schema validation failure")
                return parsed, attempt, response
            except Exception as exc:
                error = exc
                self._failure_metric(exc, stage_name)
                if attempt == self.settings.max_retries or not self._retryable(exc):
                    break
                await asyncio.sleep(min(4, .4 * (2 ** attempt)) + random.uniform(0, .15))
        assert error is not None
        raise error

    @staticmethod
    def _payload(candidate: RouteBCandidate) -> str:
        article = candidate.article
        return json.dumps({
            "candidate_id": candidate_id(candidate),
            "registered_exposure": {
                "company": candidate.company,
                "exposure_id": candidate.exposure_id,
                "subject": candidate.exposure_subject,
                "allowed_event_families": candidate.allowed_event_families,
            },
            "article": {
                "source": article.source,
                "published_at": article.published_at.isoformat() if article.published_at else None,
                "query_context": article.origin_metadata.get("query"),
                "title": article.title,
                "description": article.description,
                "text": article.text,
                "url": article.canonical_url,
            },
        }, ensure_ascii=False)

    @classmethod
    def _validate_nano(cls, candidate: RouteBCandidate, value: NanoJudgeOutput) -> None:
        if value.decision == "ESCALATE_TO_LUNA" and value.reason_code != "AMBIGUOUS":
            raise JudgeError("nano escalation must use AMBIGUOUS")
        if value.decision == "ACCEPT":
            cls._accept_parts(candidate, value.reason_code)
        if value.decision == "REJECT" and value.reason_code not in _REJECTION_REASONS:
            raise JudgeError("nano REJECT uses an unexpected reason code")

    @classmethod
    def _validate_luna(cls, candidate: RouteBCandidate, value: LunaJudgeOutput) -> None:
        if value.decision == "ACCEPT":
            cls._accept_parts(candidate, value.reason_code)
        elif value.reason_code not in _REJECTION_REASONS:
            raise JudgeError("Luna REJECT uses an unexpected reason code")

    @staticmethod
    def _accept_parts(candidate: RouteBCandidate, reason_code: str) -> tuple[str, str, str]:
        matched = _ACCEPT_REASON.fullmatch(reason_code)
        if not matched or matched.group("family") not in candidate.allowed_event_families:
            raise JudgeError("ACCEPT reason code violates registered exposure contract")
        return matched.group("family"), matched.group("materiality"), matched.group("direction")

    @classmethod
    def _decision(cls, candidate: RouteBCandidate, decision: str, reason_code: str) -> JudgeOutput:
        if decision == "ACCEPT":
            family, materiality, direction = cls._accept_parts(candidate, reason_code)
            return JudgeOutput(
                qualifies=True, company=candidate.company, exposure_id=candidate.exposure_id,
                event_family=family, materiality=materiality, impact_direction=direction,
                causal_mechanism=f"The material {family} event directly affects the registered exposure: {candidate.exposure_subject}.",
                rejection_reason="none",
            )
        return JudgeOutput(
            qualifies=False, company=candidate.company, exposure_id=candidate.exposure_id,
            event_family="none", materiality="none", impact_direction="neutral",
            causal_mechanism="The article does not establish a material link to the registered exposure.",
            rejection_reason=_REJECTION_REASONS[reason_code],
        )

    @staticmethod
    def _conservative_accept(candidate: RouteBCandidate) -> JudgeOutput:
        return JudgeOutput(
            qualifies=True, company=candidate.company, exposure_id=candidate.exposure_id,
            event_family=candidate.allowed_event_families[0], materiality="low", impact_direction="neutral",
            causal_mechanism="Conservatively retained after a Luna operational failure; semantic classification is unresolved.",
            rejection_reason="none",
        )

    def _result(
        self, candidate: RouteBCandidate, decision: JudgeOutput,
        source: Literal["nano", "luna", "luna_failure_accept"], extra: dict[str, object],
        nano: NanoJudgeOutput | None, luna: LunaJudgeOutput | None,
        escalation_reason: str | None, accepted_due_to_failure: bool,
    ) -> JudgedRouteBCandidate:
        audit = {
            "candidate_id": candidate_id(candidate),
            "nano_model": self.settings.primary_model,
            "nano_reasoning_effort": self.settings.primary_reasoning,
            "nano_decision": nano.decision if nano else "ESCALATE_TO_LUNA",
            "nano_reason_code": nano.reason_code if nano else None,
            "luna_invoked": source != "nano",
            "luna_invocation_reason": escalation_reason,
            "luna_model": self.settings.fallback_model if source != "nano" else None,
            "luna_reasoning_effort": self.settings.fallback_reasoning if source != "nano" else None,
            "luna_decision": luna.decision if luna else None,
            "luna_reason_code": luna.reason_code if luna else None,
            "accepted_due_to_classifier_failure": accepted_due_to_failure,
            "final_decision": "ACCEPT" if decision.qualifies else "REJECT",
            "final_decision_source": source,
            **extra,
        }
        model = self.settings.primary_model if source == "nano" else self.settings.fallback_model
        prompt = NANO_PROMPT_VERSION if source == "nano" else LUNA_PROMPT_VERSION
        return JudgedRouteBCandidate(candidate, decision, prompt, model, audit)

    def _failure_metric(self, exc: Exception, stage_name: str) -> None:
        stage = self.metrics.nano if stage_name == "nano" else self.metrics.luna
        text = str(exc).casefold()
        status = getattr(exc, "status_code", None)
        if status == 429 or "429" in text:
            stage.rate_limits += 1
        if isinstance(exc, TimeoutError) or "timeout" in text:
            stage.timeouts += 1
        if (isinstance(status, int) and status >= 500) or "5xx" in text or "server error" in text:
            stage.server_errors += 1
        if "schema" in text or "malformed" in text or "unexpected" in text or "violates" in text:
            stage.schema_failures += 1

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        text = str(exc).casefold()
        status = getattr(exc, "status_code", None)
        return (
            isinstance(exc, TimeoutError)
            or status == 429
            or (isinstance(status, int) and status >= 500)
            or any(value in text for value in ("429", "5xx", "server error", "connection", "timeout"))
        )

    @staticmethod
    def _reason(exc: Exception, stage_name: str) -> str:
        text = str(exc).casefold()
        status = getattr(exc, "status_code", None)
        if isinstance(exc, TimeoutError) or "timeout" in text:
            return f"{stage_name}_timeout"
        if status == 429 or "429" in text:
            return f"{stage_name}_429_exhausted"
        if (isinstance(status, int) and status >= 500) or "5xx" in text or "server error" in text:
            return f"{stage_name}_5xx_exhausted"
        if "schema" in text or "malformed" in text or "unexpected" in text or "violates" in text:
            return f"{stage_name}_schema_failure"
        return f"{stage_name}_client_error"

    @staticmethod
    def _usage(counter: Counter[str], response: Any) -> None:
        usage = getattr(response, "usage", None)
        for field_name in ("input_tokens", "output_tokens"):
            value = getattr(usage, field_name, None) if usage else None
            if isinstance(value, int):
                counter[field_name] += value
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", None) if usage else None
        if isinstance(cached, int):
            counter["cached_input_tokens"] += cached
        reasoning = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", None) if usage else None
        if isinstance(reasoning, int):
            counter["reasoning_tokens"] += reasoning
