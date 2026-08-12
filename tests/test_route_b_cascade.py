from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from companyk_newsbot.judges import CascadeSettings, JudgeOutput, LunaJudgeOutput, RouteBCascadeJudge
from companyk_newsbot.models import Article
from companyk_newsbot.rules import RouteBCandidate


def candidate(index: int = 1) -> RouteBCandidate:
    return RouteBCandidate(
        article=Article(source="source", source_type="rss", title=f"Article {index}", url=f"https://example.com/{index}", canonical_url=f"https://example.com/{index}", description="Relevant policy event", published_at=datetime(2026, 8, 12, tzinfo=UTC), retrieved_at=datetime(2026, 8, 12, tzinfo=UTC)),
        company="Example Co", exposure_id="example", exposure_subject="Example policy", allowed_event_families=("policy",),
    )


def luna(decision: str) -> LunaJudgeOutput:
    return LunaJudgeOutput(decision=decision, reason_code="none" if decision == "ACCEPT" else "broad_industry_only" if decision == "REJECT" else "uncertain", short_reason="test reason", confidence="high" if decision != "ESCALATE_TO_SOL" else "low", uncertainty_flags=[] if decision != "ESCALATE_TO_SOL" else ["ambiguous"], event_family="policy" if decision == "ACCEPT" else "none", materiality="high" if decision == "ACCEPT" else "none", impact_direction="negative" if decision == "ACCEPT" else "neutral", causal_mechanism="A company-specific causal path exists." if decision == "ACCEPT" else "The evidence is not sufficient.")


def sol(qualifies: bool = True) -> JudgeOutput:
    return JudgeOutput(qualifies=qualifies, company="Example Co", exposure_id="example", event_family="policy" if qualifies else "none", materiality="high" if qualifies else "none", impact_direction="negative" if qualifies else "neutral", causal_mechanism="Sol decision", rejection_reason="none" if qualifies else "wrong_context")


class Responses:
    def __init__(self, values): self.values = list(values); self.calls = 0
    async def parse(self, **_kwargs):
        value = self.values[self.calls]; self.calls += 1
        if isinstance(value, Exception): raise value
        await asyncio.sleep(0)
        return SimpleNamespace(output_parsed=value, usage=SimpleNamespace(input_tokens=1, output_tokens=1, input_tokens_details=SimpleNamespace(cached_tokens=0)))


class Client:
    def __init__(self, values): self.responses = Responses(values)


def settings() -> CascadeSettings:
    return CascadeSettings(luna_concurrency=2, sol_concurrency=1, luna_rpm_budget=1000, sol_rpm_budget=1000, max_retries=1)


def test_luna_final_accept_and_reject_do_not_call_sol() -> None:
    primary, fallback = Client([luna("ACCEPT"), luna("REJECT")]), Client([])
    results = RouteBCascadeJudge(primary, fallback, settings()).judge_all_sync([candidate(1), candidate(2)])
    assert [result.audit["final_decision_source"] for result in results] == ["luna", "luna"]
    assert fallback.responses.calls == 0


def test_luna_escalation_and_timeout_call_sol_without_silent_rejection() -> None:
    primary, fallback = Client([luna("ESCALATE_TO_SOL"), TimeoutError("timeout"), TimeoutError("timeout")]), Client([sol(True), sol(False)])
    cascade = RouteBCascadeJudge(primary, fallback, settings())
    results = cascade.judge_all_sync([candidate(1), candidate(2)])
    assert fallback.responses.calls == 2
    assert {result.audit["sol_invocation_reason"] for result in results} == {"luna_uncertain", "luna_timeout"}
    assert cascade.metrics.luna_error_fallbacks == 1
    assert cascade.metrics.sol_accepts == 1 and cascade.metrics.sol_rejects == 1


def test_terminal_sol_failure_is_explicitly_unresolved() -> None:
    primary, fallback = Client([luna("ESCALATE_TO_SOL")]), Client([RuntimeError("network error"), RuntimeError("network error")])
    result = RouteBCascadeJudge(primary, fallback, settings()).judge_all_sync([candidate()])[0]
    assert result.audit["final_decision_source"] == "unresolved"
    assert result.audit["final_decision"] == "UNRESOLVED"


def test_completion_order_does_not_misassociate_results() -> None:
    primary, fallback = Client([luna("REJECT"), luna("ACCEPT")]), Client([])
    results = RouteBCascadeJudge(primary, fallback, settings()).judge_all_sync([candidate(9), candidate(3)])
    assert {result.candidate.article.title: result.audit["final_decision"] for result in results} == {"Article 9": "REJECT", "Article 3": "ACCEPT"}
