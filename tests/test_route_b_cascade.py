from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from companyk_newsbot.judges import CascadeSettings, LunaJudgeOutput, NanoJudgeOutput, RouteBCascadeJudge
from companyk_newsbot.models import Article
from companyk_newsbot.rules import RouteBCandidate


def candidate(index: int = 1) -> RouteBCandidate:
    return RouteBCandidate(
        article=Article(
            source="source", source_type="rss", title=f"Article {index}",
            url=f"https://example.com/{index}", canonical_url=f"https://example.com/{index}",
            description="Relevant policy event", text="The policy directly affects the registered exposure.",
            published_at=datetime(2026, 8, 12, tzinfo=UTC), retrieved_at=datetime(2026, 8, 12, tzinfo=UTC),
            origin_metadata={"query": "Example policy"},
        ),
        company="Example Co", exposure_id="example", exposure_subject="Example policy",
        allowed_event_families=("policy",),
    )


def nano(decision: str) -> NanoJudgeOutput:
    reasons = {
        "ACCEPT": "MATERIAL_LINK|policy|high|negative",
        "REJECT": "NO_MATERIAL_LINK",
        "ESCALATE_TO_LUNA": "AMBIGUOUS",
    }
    return NanoJudgeOutput(decision=decision, reason_code=reasons[decision])


def luna(decision: str) -> LunaJudgeOutput:
    return LunaJudgeOutput(
        decision=decision,
        reason_code="MATERIAL_LINK|policy|medium|negative" if decision == "ACCEPT" else "WRONG_CONTEXT",
    )


class StatusError(RuntimeError):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class Responses:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0
        self.kwargs = []

    async def parse(self, **kwargs):
        self.kwargs.append(kwargs)
        value = self.values[self.calls]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        await asyncio.sleep(0)
        return SimpleNamespace(
            output_parsed=value,
            usage=SimpleNamespace(
                input_tokens=11, output_tokens=2,
                input_tokens_details=SimpleNamespace(cached_tokens=5),
                output_tokens_details=SimpleNamespace(reasoning_tokens=1),
            ),
        )


class Client:
    def __init__(self, values):
        self.responses = Responses(values)


def settings() -> CascadeSettings:
    return CascadeSettings(
        nano_concurrency=2, luna_concurrency=1,
        nano_rpm_budget=1000, luna_rpm_budget=1000, max_retries=0,
    )


def test_nano_accept_and_reject_bypass_luna() -> None:
    primary, fallback = Client([nano("ACCEPT"), nano("REJECT")]), Client([])
    results = RouteBCascadeJudge(primary, fallback, settings()).judge_all_sync([candidate(1), candidate(2)])
    assert [result.audit["final_decision_source"] for result in results] == ["nano", "nano"]
    assert {result.audit["final_decision"] for result in results} == {"ACCEPT", "REJECT"}
    assert fallback.responses.calls == 0
    assert set(primary.responses.kwargs[0]["text_format"].model_fields) == {"decision", "reason_code"}


def test_nano_escalation_calls_luna() -> None:
    primary, fallback = Client([nano("ESCALATE_TO_LUNA")]), Client([luna("ACCEPT")])
    result = RouteBCascadeJudge(primary, fallback, settings()).judge_all_sync([candidate()])[0]
    assert fallback.responses.calls == 1
    assert result.audit["luna_invocation_reason"] == "nano_ambiguous"
    assert result.audit["final_decision"] == "ACCEPT"


@pytest.mark.parametrize(
    "failure, reason",
    [
        (TimeoutError("timeout"), "nano_timeout"),
        (StatusError(429), "nano_429_exhausted"),
        (StatusError(503), "nano_5xx_exhausted"),
        (RuntimeError("malformed structured output"), "nano_schema_failure"),
        (RuntimeError("schema validation failure"), "nano_schema_failure"),
        (RuntimeError("unexpected enum"), "nano_schema_failure"),
        (RuntimeError("client exception"), "nano_client_error"),
    ],
)
def test_nano_operational_failure_fails_open_to_luna(failure: Exception, reason: str) -> None:
    primary, fallback = Client([failure]), Client([luna("REJECT")])
    cascade = RouteBCascadeJudge(primary, fallback, settings())
    result = cascade.judge_all_sync([candidate()])[0]
    assert fallback.responses.calls == 1
    assert result.audit["luna_invocation_reason"] == reason
    assert result.audit["final_decision"] == "REJECT"
    assert cascade.metrics.nano.operational_failures == 1


@pytest.mark.parametrize("decision", ["ACCEPT", "REJECT"])
def test_luna_final_decisions_work(decision: str) -> None:
    result = RouteBCascadeJudge(
        Client([nano("ESCALATE_TO_LUNA")]), Client([luna(decision)]), settings()
    ).judge_all_sync([candidate()])[0]
    assert result.audit["final_decision"] == decision
    assert result.audit["accepted_due_to_classifier_failure"] is False


def test_luna_operational_failure_is_conservative_accept() -> None:
    cascade = RouteBCascadeJudge(
        Client([nano("ESCALATE_TO_LUNA")]), Client([RuntimeError("client exception")]), settings()
    )
    result = cascade.judge_all_sync([candidate()])[0]
    assert result.decision.qualifies is True
    assert result.audit["final_decision_source"] == "luna_failure_accept"
    assert result.audit["accepted_due_to_classifier_failure"] is True
    assert cascade.metrics.accepted_due_to_classifier_failure == 1


def test_usage_telemetry_is_observational_and_complete() -> None:
    cascade = RouteBCascadeJudge(Client([nano("ACCEPT")]), Client([]), settings())
    result = cascade.judge_all_sync([candidate()])[0]
    metrics = cascade.metrics.payload()
    assert result.audit["final_decision"] == "ACCEPT"
    assert metrics["nano_input_tokens"] == 11
    assert metrics["nano_cached_input_tokens"] == 5
    assert metrics["nano_output_tokens"] == 2
    assert metrics["nano_reasoning_tokens"] == 1
    assert metrics["production_sol_calls"] == 0


def test_completion_order_does_not_misassociate_results() -> None:
    primary, fallback = Client([nano("REJECT"), nano("ACCEPT")]), Client([])
    results = RouteBCascadeJudge(primary, fallback, settings()).judge_all_sync([candidate(9), candidate(3)])
    assert {result.candidate.article.title: result.audit["final_decision"] for result in results} == {
        "Article 9": "REJECT", "Article 3": "ACCEPT",
    }
