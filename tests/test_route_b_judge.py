from __future__ import annotations

from datetime import UTC, datetime
import json
from types import SimpleNamespace

import pytest

from companyk_newsbot.judges import JudgeError, JudgeOutput, RouteBCausalMaterialityJudge
from companyk_newsbot.models import Article
from companyk_newsbot.rules import RouteBCandidate


class FakeResponses:
    def __init__(self, output: JudgeOutput) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.output)


class FakeClient:
    def __init__(self, output: JudgeOutput) -> None:
        self.responses = FakeResponses(output)


def candidate() -> RouteBCandidate:
    return RouteBCandidate(
        article=Article(
            source="Official source",
            source_type="official_rss",
            title="Google Play announces billing fee change",
            url="https://example.com/article",
            canonical_url="https://example.com/article",
            description="The billing fee will change next month.",
            published_at=datetime(2026, 8, 11, tzinfo=UTC),
            retrieved_at=datetime(2026, 8, 12, tzinfo=UTC),
        ),
        company="Example Co",
        exposure_id="example_google_play",
        exposure_subject="Google Play billing exposure",
        allowed_event_families=("platform_infrastructure_dependency",),
    )


def decision(**overrides: object) -> JudgeOutput:
    data: dict[str, object] = {
        "qualifies": True,
        "company": "Example Co",
        "exposure_id": "example_google_play",
        "event_family": "platform_infrastructure_dependency",
        "materiality": "high",
        "impact_direction": "negative",
        "causal_mechanism": "Higher platform fees can reduce the company's unit economics.",
        "rejection_reason": "none",
    }
    data.update(overrides)
    return JudgeOutput.model_validate(data)


def test_judge_submits_only_candidate_context_and_returns_valid_structured_decision() -> None:
    client = FakeClient(decision())
    result = RouteBCausalMaterialityJudge(client, model="test-model").judge(candidate())

    assert result.decision.qualifies is True
    assert result.prompt_version == "route_b_causal_materiality_v1"
    call = client.responses.calls[0]
    payload = json.loads(call["input"][1]["content"])
    assert payload["registered_exposure"]["exposure_id"] == "example_google_play"
    assert payload["article"]["title"] == "Google Play announces billing fee change"
    assert call["text_format"] is JudgeOutput


def test_judge_accepts_explicit_broad_industry_rejection() -> None:
    rejected = decision(
        qualifies=False,
        event_family="none",
        materiality="none",
        impact_direction="neutral",
        causal_mechanism="The article is broad market commentary without the registered platform event.",
        rejection_reason="broad_industry_only",
    )
    result = RouteBCausalMaterialityJudge(FakeClient(rejected), model="test-model").judge(candidate())
    assert result.decision.rejection_reason == "broad_industry_only"


def test_judge_rejects_unregistered_event_family_or_candidate_identity() -> None:
    wrong_family = decision(event_family="competition")
    with pytest.raises(JudgeError, match="not allowed"):
        RouteBCausalMaterialityJudge(FakeClient(wrong_family), model="test-model").judge(candidate())

    wrong_company = decision(company="Other Co")
    with pytest.raises(JudgeError, match="company different"):
        RouteBCausalMaterialityJudge(FakeClient(wrong_company), model="test-model").judge(candidate())


def test_judge_requires_model_configuration() -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        RouteBCausalMaterialityJudge(FakeClient(decision()), model=" ")
