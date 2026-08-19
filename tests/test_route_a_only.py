from datetime import UTC, datetime
import json
from pathlib import Path
import pytest

from companyk_newsbot.dedup import article_id
from companyk_newsbot.judges.direct_event import DirectEventAssessment, DirectGroundingVerdict
from companyk_newsbot.models import Article
from companyk_newsbot.portfolio_registry import PortfolioRegistry
from companyk_newsbot.route_a_only import process_route_a_articles


NOW = datetime(2026, 8, 14, tzinfo=UTC)


def registry() -> PortfolioRegistry:
    return PortfolioRegistry.model_validate({"schema_version": "1", "source": {"workbook": "fixture.xlsx", "sheet": "S", "column": "A",
        "source_sha256": "0" * 64, "generated_at": "2026-08-14T00:00:00", "company_count": 2}, "companies": [
        {"company_id": "company-aaaaaaaa", "display_name": "AlphaBio", "source_name": "AlphaBio Ltd.", "legal_names": ["AlphaBio Ltd."],
         "former_names": [], "search_terms": ["AlphaBio"], "match_terms": ["AlphaBio"], "ambiguity": {}},
        {"company_id": "company-bbbbbbbb", "display_name": "BetaTech", "source_name": "BetaTech", "legal_names": ["BetaTech"],
         "former_names": [], "search_terms": ["BetaTech"], "match_terms": ["BetaTech"], "ambiguity": {}}]})


def article(company: str, slug: str, title_suffix: str) -> Article:
    return Article(source="fixture", source_type="fixture", title=f"{company} {title_suffix}", url=f"https://e/{slug}", canonical_url=f"https://e/{slug}",
        description=f"{company} announced {title_suffix}", published_at=NOW, retrieved_at=NOW, origin_metadata={"query": company})


class FakeJudge:
    def __init__(self): self.calls = []
    def assess(self, event):
        self.calls.append(event.event_id)
        if event.company == "BetaTech":
            return DirectEventAssessment(decision="IGNORE", reason_code="minor_award", materiality="none", event_family="other",
                fact_summary=None, investor_insight=None, evidence_article_ids=[])
        return DirectEventAssessment(decision="DELIVER", reason_code="funding", materiality="high", event_family="financing",
            fact_summary="알파바이오가 신규 투자를 유치했습니다.", investor_insight=None,
            evidence_article_ids=[article_id(event.primary.article)])


class FakeGrounder:
    def __init__(self, unsupported=False): self.calls = []; self.unsupported = unsupported
    def ground(self, event, assessment):
        self.calls.append(event.event_id)
        verdict = DirectGroundingVerdict(fact_summary="UNSUPPORTED" if self.unsupported else "SUPPORTED", investor_insight="NOT_PRESENT", unsupported_claims=[])
        return (event.primary.article.title if self.unsupported else assessment.fact_summary), None, verdict


def test_dedup_and_assessment_before_ranking_then_ground_final_only() -> None:
    funding = article("AlphaBio", "funding", "raises funding")
    award = article("BetaTech", "award", "wins routine award")
    judge, grounder = FakeJudge(), FakeGrounder()
    result = process_route_a_articles([funding, funding.model_copy(), award], registry(), judge=judge, grounder=grounder)
    assert result.article_duplicates == 1
    assert len(judge.calls) == len(result.events) == 2
    assert result.ignore_count == 1 and result.deliver_high == 1
    assert len(result.ranked_items) == len(result.email_items) == len(grounder.calls) == 1
    assert result.email_items[0].summary.insight_one_liner is None


def registry_many(count: int) -> PortfolioRegistry:
    companies = [{
        "company_id": f"company-{index:08d}", "display_name": f"Company{index}", "source_name": f"Company{index}",
        "legal_names": [f"Company{index}"], "former_names": [], "search_terms": [f"Company{index}"],
        "match_terms": [f"Company{index}"], "ambiguity": {},
    } for index in range(count)]
    return PortfolioRegistry.model_validate({
        "schema_version": "1",
        "source": {"workbook": "fixture.xlsx", "sheet": "S", "column": "A", "source_sha256": "0" * 64,
                   "generated_at": "2026-08-14T00:00:00", "company_count": count},
        "companies": companies,
    })


class AlwaysDeliverJudge:
    def __init__(self): self.calls = 0
    def assess(self, event):
        self.calls += 1
        return DirectEventAssessment(
            decision="DELIVER", reason_code="material_event", materiality="medium", event_family="other",
            fact_summary=f"{event.company} material event.", investor_insight="Optional proposed insight.",
            evidence_article_ids=[article_id(event.primary.article)],
        )


@pytest.mark.parametrize(("input_count", "expected"), [(0, 0), (1, 1), (7, 7), (12, 12), (13, 13), (20, 20)])
def test_dynamic_ranking_never_pads_or_truncates_delivery_events(input_count: int, expected: int) -> None:
    source = [article(f"Company{index}", f"event-{index}", "announces material event") for index in range(input_count)]
    judge, grounder = AlwaysDeliverJudge(), FakeGrounder()
    result = process_route_a_articles(source, registry_many(input_count) if input_count else registry(), judge=judge, grounder=grounder)
    assert len(result.ranked_items) == len(result.email_items) == expected
    assert judge.calls == input_count
    assert len(grounder.calls) == expected


def test_empty_input_is_a_valid_zero_item_briefing() -> None:
    judge, grounder = AlwaysDeliverJudge(), FakeGrounder()
    result = process_route_a_articles([], registry(), judge=judge, grounder=grounder)
    assert result.ranked_items == result.email_items == ()
    assert judge.calls == 0 and grounder.calls == []


def test_one_grounding_failure_is_isolated_and_never_emails_that_event() -> None:
    class FlakyGrounder(FakeGrounder):
        def ground(self, event, assessment):
            if event.company == "AlphaBio":
                raise RuntimeError("grounding unavailable")
            return super().ground(event, assessment)

    source = [article("AlphaBio", "a", "material event"), article("BetaTech", "b", "material event")]
    result = process_route_a_articles(source, registry(), judge=AlwaysDeliverJudge(), grounder=FlakyGrounder())
    assert result.model_failure_events == 1
    assert [item.item.company for item in result.email_items] == ["BetaTech"]
    assert result.systemic_model_failure is False


def test_multiple_articles_in_one_proto_event_get_one_assessment() -> None:
    first = article("AlphaBio", "funding-a", "raises $10m Series B funding")
    second = first.model_copy(update={
        "url": "https://e/funding-b", "canonical_url": "https://e/funding-b", "source": "second",
        "title": "AlphaBio raises $10m Series B funding round",
    })
    judge, grounder = AlwaysDeliverJudge(), FakeGrounder()
    result = process_route_a_articles([first, second], registry(), judge=judge, grounder=grounder)
    assert len(result.events) == 1
    assert result.events[0].coverage_count == 2
    assert judge.calls == len(grounder.calls) == len(result.email_items) == 1


def test_three_distinct_events_for_one_company_all_survive_ordering() -> None:
    source = [
        article("AlphaBio", "funding", "raises Series B funding"),
        article("AlphaBio", "approval", "wins regulatory approval"),
        article("AlphaBio", "partnership", "signs strategic partnership"),
    ]
    judge, grounder = AlwaysDeliverJudge(), FakeGrounder()
    result = process_route_a_articles(source, registry(), judge=judge, grounder=grounder)
    assert len(result.events) == len(result.ranked_items) == len(result.email_items) == len(grounder.calls) == 3


def test_unsupported_investor_insight_is_omitted() -> None:
    class UnsupportedInsightGrounder(FakeGrounder):
        def ground(self, event, assessment):
            self.calls.append(event.event_id)
            verdict = DirectGroundingVerdict(
                fact_summary="SUPPORTED", investor_insight="UNSUPPORTED", unsupported_claims=["speculative impact"]
            )
            return assessment.fact_summary, None, verdict

    result = process_route_a_articles(
        [article("AlphaBio", "funding", "raises funding")],
        registry(),
        judge=AlwaysDeliverJudge(),
        grounder=UnsupportedInsightGrounder(),
    )
    assert result.email_items[0].summary.insight_one_liner is None
    assert result.grounding_verdicts[result.ranked_items[0].event_id].investor_insight == "UNSUPPORTED"


def test_direct_event_regression_fixture_contracts_are_strict() -> None:
    fixture = json.loads(Path("tests/fixtures/direct_event_assessments.json").read_text(encoding="utf-8"))
    assert len(fixture["deliver"]) == len(fixture["ignore"]) == 8
    for index, value in enumerate(fixture["deliver"]):
        parsed = DirectEventAssessment(
            decision="DELIVER", reason_code=value["category"], materiality=value["materiality"],
            event_family=value["event_family"], fact_summary=f"Supported fixture fact {index}.",
            investor_insight=None, evidence_article_ids=[f"evidence-{index}"],
        )
        assert parsed.decision == "DELIVER" and parsed.materiality != "none"
    for value in fixture["ignore"]:
        parsed = DirectEventAssessment(
            decision="IGNORE", reason_code=value["reason_code"], materiality="none", event_family="other",
            fact_summary=None, investor_insight=None, evidence_article_ids=[],
        )
        assert parsed.decision == "IGNORE" and parsed.fact_summary is None


def test_unsupported_fact_uses_deterministic_title_and_never_delivers_insight() -> None:
    funding = article("AlphaBio", "funding", "raises funding")
    result = process_route_a_articles([funding], registry(), judge=FakeJudge(), grounder=FakeGrounder(unsupported=True))
    assert result.email_items[0].summary.fact_summary == funding.title
    assert result.email_items[0].summary.insight_one_liner is None
