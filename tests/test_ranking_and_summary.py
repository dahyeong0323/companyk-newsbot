from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from companyk_newsbot.judges import JudgeOutput, JudgedRouteBCandidate, NewsSummarizer, SummaryError, SummaryOutput
from companyk_newsbot.dedup.event import article_id
from companyk_newsbot.models import Article
from companyk_newsbot.ranking import NewsRanker, RankedNewsItem
from companyk_newsbot.rules import RouteAMatch, RouteBCandidate


def article(title: str, when: int = 8) -> Article:
    return Article(source="test", source_type="fixture", title=title, url=f"https://example.com/{title}", canonical_url=f"https://example.com/{title}", retrieved_at=datetime(2026, 8, 12, tzinfo=UTC), published_at=datetime(2026, 8, 11, when, tzinfo=UTC), description="Test description")


def direct(company: str, title: str, materiality: str = "medium") -> RankedNewsItem:
    return RankedNewsItem.from_direct(RouteAMatch(company, (company,), article(title)), materiality=materiality)  # type: ignore[arg-type]


def external(company: str, title: str, materiality: str = "high") -> RankedNewsItem:
    candidate = RouteBCandidate(article(title), company, "exposure", "subject", ("competition",))
    decision = JudgeOutput(qualifies=True, company=company, exposure_id="exposure", event_family="competition", materiality=materiality, impact_direction="mixed", causal_mechanism="Specific causal mechanism.", rejection_reason="none")  # type: ignore[arg-type]
    return RankedNewsItem.from_external(JudgedRouteBCandidate(candidate, decision, "test", "test"))


def grounded(item: RankedNewsItem, *, why: str | None = None) -> SummaryOutput:
    article_value = item.direct_match.article if item.direct_match else item.external_match.candidate.article
    return SummaryOutput(summary="Factual summary.", why_it_matters=why, insight_one_liner="The concrete next variable is execution.", insight_dimension="strategy", insight_mode="watchpoint", confidence="medium", evidence_article_ids=[article_id(article_value)])


def test_ranker_applies_route_materiality_order_and_company_cap() -> None:
    items = [direct("A", "direct medium"), external("B", "external high"), direct("C", "direct high", "high"), external("D", "external medium", "medium"), direct("A", "second direct"), direct("A", "third direct")]
    ranked = NewsRanker(total_max_items=10, max_items_per_company=2).rank(items)
    assert [(item.route, item.materiality, item.company) for item in ranked] == [("direct", "high", "C"), ("external", "high", "B"), ("direct", "medium", "A"), ("direct", "medium", "A"), ("external", "medium", "D")]


class FakeResponses:
    def __init__(self, output: SummaryOutput) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []
    def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.output)


class FakeClient:
    def __init__(self, output: SummaryOutput) -> None:
        self.responses = FakeResponses(output)


def test_summarizer_requires_why_for_external_and_hides_internal_ids() -> None:
    item = external("A", "fee change")
    client = FakeClient(grounded(item, why="Margin pressure is approved context."))
    result = NewsSummarizer(client, model="test").summarize(item)
    assert result.why_it_matters
    payload = client.responses.calls[0]["input"][1]["content"]
    assert "exposure_id" not in payload
    assert '"exposure"' not in payload
    return
    client = FakeClient(SummaryOutput(summary="플랫폼 수수료가 변경됐다.", why_it_matters="수익성에 영향을 줄 수 있다."))
    result = NewsSummarizer(client, model="test").summarize(external("A", "fee change"))
    assert result.why_it_matters
    payload = client.responses.calls[0]["input"][1]["content"]
    assert "exposure_id" not in payload
    assert '"exposure"' not in payload


def test_summarizer_rejects_wrong_route_summary_shape() -> None:
    with pytest.raises(SummaryError, match="must include why"):
        NewsSummarizer(FakeClient(SummaryOutput(summary="요약")), model="test").summarize(external("A", "fee change"))
    with pytest.raises(SummaryError, match="must not include"):
        NewsSummarizer(FakeClient(SummaryOutput(summary="요약", why_it_matters="불필요")), model="test").summarize(direct("A", "direct"))
