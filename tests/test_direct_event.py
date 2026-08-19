from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from companyk_newsbot.dedup import RouteAEventClusterer, article_id
from companyk_newsbot.judges.direct_event import DirectEventAssessment, DirectEventJudge
from companyk_newsbot.models import Article
from companyk_newsbot.rules import RouteAMatch


def event():
    article = Article(
        source="fixture",
        source_type="fixture",
        title="AlphaBio raises Series B funding",
        url="https://example.com/funding",
        canonical_url="https://example.com/funding",
        description="AlphaBio announced Series B funding.",
        published_at=datetime(2026, 8, 18, tzinfo=UTC),
        retrieved_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    match = RouteAMatch("AlphaBio", ("AlphaBio",), article)
    return RouteAEventClusterer().cluster([match])[0]


class Responses:
    def __init__(self, assessment: DirectEventAssessment) -> None:
        self.assessment = assessment
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.assessment, usage=None)


def test_unknown_assessment_evidence_is_ignored_without_stopping_the_batch() -> None:
    cluster = event()
    malformed = DirectEventAssessment(
        decision="DELIVER",
        reason_code="funding",
        materiality="high",
        event_family="financing",
        fact_summary="알파바이오가 신규 투자를 유치했습니다.",
        investor_insight=None,
        evidence_article_ids=["not-an-event-article"],
    )
    responses = Responses(malformed)
    judge = DirectEventJudge(SimpleNamespace(responses=responses), model="test")

    assessment = judge.assess(cluster)

    assert assessment.decision == "IGNORE"
    assert assessment.reason_code == "invalid_evidence_id"
    assert assessment.evidence_article_ids == []
    assert judge.metrics.calls == 1
    assert judge.metrics.failures == 0
    assert judge.metrics.invalid_evidence_fallbacks == 1


def test_valid_assessment_evidence_remains_deliverable() -> None:
    cluster = event()
    valid = DirectEventAssessment(
        decision="DELIVER",
        reason_code="funding",
        materiality="high",
        event_family="financing",
        fact_summary="알파바이오가 신규 투자를 유치했습니다.",
        investor_insight=None,
        evidence_article_ids=[article_id(cluster.primary.article)],
    )
    judge = DirectEventJudge(SimpleNamespace(responses=Responses(valid)), model="test")

    assert judge.assess(cluster) == valid
    assert judge.metrics.invalid_evidence_fallbacks == 0


def test_assessment_schema_or_api_failure_retries_once() -> None:
    cluster = event()
    valid = DirectEventAssessment(
        decision="DELIVER", reason_code="funding", materiality="high", event_family="financing",
        fact_summary="알파바이오가 신규 투자를 유치했습니다.", investor_insight=None,
        evidence_article_ids=[article_id(cluster.primary.article)],
    )

    class FlakyResponses:
        calls = 0
        def parse(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary API failure")
            return SimpleNamespace(output_parsed=valid, usage=None)

    responses = FlakyResponses()
    judge = DirectEventJudge(SimpleNamespace(responses=responses), model="test")
    assert judge.assess(cluster) == valid
    assert responses.calls == 2
    assert judge.metrics.retries == 1 and judge.metrics.failures == 0
