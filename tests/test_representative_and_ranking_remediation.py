from __future__ import annotations

from datetime import UTC, datetime
from itertools import permutations

import pytest

from companyk_newsbot.dedup import RepresentativeArticleSelector, RouteAEventClusterer, RouteBEventClusterer, article_id
from companyk_newsbot.judges import JudgeOutput, JudgedRouteBCandidate
from companyk_newsbot.models import Article
from companyk_newsbot.ranking import NewsRanker, RankedNewsItem
from companyk_newsbot.rules import RouteAMatch, RouteBCandidate


NOW = datetime(2026, 8, 11, 8, tzinfo=UTC)


def article(
    title: str,
    url: str,
    *,
    source: str = "Publisher",
    hour: int = 8,
    description: str | None = None,
    text: str | None = None,
    redirect: bool = False,
) -> Article:
    return Article(
        source=source,
        source_type="fixture",
        title=title,
        url=url,
        canonical_url=url,
        published_at=NOW.replace(hour=hour),
        retrieved_at=NOW,
        description=description,
        text=text,
        origin_metadata={"redirect_url": "https://target.example/story"} if redirect else {},
    )


def judged(value: Article, company: str, exposure: str, materiality: str = "medium") -> JudgedRouteBCandidate:
    candidate = RouteBCandidate(value, company, exposure, "subject", ("competition",))
    decision = JudgeOutput(
        qualifies=True,
        company=company,
        exposure_id=exposure,
        event_family="competition",
        materiality=materiality,
        impact_direction="mixed",
        causal_mechanism="Approved mechanism.",
        rejection_reason="none",
    )
    return JudgedRouteBCandidate(candidate, decision, "test", "test")


@pytest.mark.parametrize(
    ("preferred", "other"),
    [
        (
            article("FDA approves Acme therapy", "https://fda.gov/acme", text="Official approval record with trial and date details. " * 4),
            article("FDA approves Acme therapy", "https://news.yahoo.com/acme", source="Yahoo News", text="Long aggregator rewrite. " * 50),
        ),
        (
            article("Acme raises funding", "https://publisher.example/acme", text="Direct reporting with named investors and terms. " * 5),
            article("Acme raises funding", "https://portal.example/acme", text="Portal copy. " * 20, redirect=True),
        ),
        (
            article("Acme raises $10 million", "https://publisher.example/rich", hour=9, description="Acme raised $10 million from Alpha on 2026-08-10.", text="Acme raised $10 million from Alpha on 2026-08-10. " * 5),
            article("Acme raises $10 million", "https://publisher.example/thin", hour=8),
        ),
        (
            article("Acme partnership with Alpha", "https://publisher.example/body", text="Substantive article body. " * 15),
            article("Acme partnership with Alpha", "https://publisher.example/title-only"),
        ),
    ],
)
def test_representative_quality_signals_choose_preferred_article(preferred: Article, other: Article) -> None:
    chosen, _, scores = RepresentativeArticleSelector().choose([other, preferred], lambda value: value)
    assert chosen == preferred
    assert scores[article_id(preferred)].total > scores[article_id(other)].total


def test_equal_score_tie_break_is_stable_and_input_order_independent() -> None:
    early_url = article("Acme exact event", "https://a.example/story")
    later_url = article("Acme exact event", "https://b.example/story")
    selector = RepresentativeArticleSelector()
    selected = [selector.choose(order, lambda value: value)[0].canonical_url for order in permutations([early_url, later_url])]
    assert selected == ["https://a.example/story", "https://a.example/story"]


def test_complete_score_breakdown_total_is_retained() -> None:
    value = article("Acme raises $10 million", "https://publisher.example/rich", description="Acme raised $10 million from Alpha.", text="Detailed body. " * 20)
    score = RepresentativeArticleSelector().score(value)
    payload = score.payload()
    assert payload["total"] == sum(value for key, value in payload.items() if key != "total")
    assert set(payload) == {"source_of_record", "direct_publisher", "body_completeness", "description_completeness", "numeric_detail", "named_factual_richness", "aggregator_penalty", "title_only_penalty", "total"}


def test_shared_selector_is_used_by_both_routes_and_all_coverage_is_retained() -> None:
    official = article("FDA approves Acme therapy on 2026-08-10", "https://fda.gov/acme", text="Official complete record. " * 10)
    aggregator = article("Acme therapy gets FDA approval on 2026-08-10", "https://news.yahoo.com/acme", source="Yahoo", text="Aggregator. " * 50)
    route_a = RouteAEventClusterer().cluster([RouteAMatch("Acme", ("Acme",), aggregator), RouteAMatch("Acme", ("Acme",), official)])[0]
    route_b = RouteBEventClusterer().cluster([judged(aggregator, "Acme", "one"), judged(official, "Acme", "two")])[0]
    assert route_a.primary.article == official
    assert route_b.representative.candidate.article == official
    assert route_a.coverage_count == route_b.coverage_count == 2
    assert len(route_a.representative_scores) == len(route_b.representative_scores) == 2


def make_multi_event(order: tuple[str, ...] = ("A", "B")):
    shared = article("Platform announces $10 million fee settlement", "https://publisher.example/event")
    links = {
        "A": judged(shared, "A", "a", "medium"),
        "B": judged(shared, "B", "b", "high"),
    }
    return RouteBEventClusterer().cluster([links[key] for key in order])[0]


def test_route_b_materiality_aggregates_all_impact_links() -> None:
    event = make_multi_event()
    assert event.materiality == "high"
    assert len(event.impact_links) == 2
    assert event.companies == ("A", "B")


def test_route_b_event_and_ranking_are_input_order_independent() -> None:
    forward, reverse = make_multi_event(("A", "B")), make_multi_event(("B", "A"))
    assert (forward.event_id, forward.materiality, forward.companies) == (reverse.event_id, reverse.materiality, reverse.companies)
    assert RankedNewsItem.from_external_event(forward) == RankedNewsItem.from_external_event(reverse)


def test_multi_company_event_consumes_one_global_slot() -> None:
    item = RankedNewsItem.from_external_event(make_multi_event())
    ranked = NewsRanker(total_max_items=1, max_items_per_company=2).rank([item])
    assert len(ranked) == 1
    assert ranked[0].impacted_companies == ("A", "B")


def test_multi_company_event_counts_against_every_company_cap() -> None:
    multi = RankedNewsItem.from_external_event(make_multi_event())
    b_event = RouteBEventClusterer().cluster([judged(article("B separate $20 million event", "https://publisher.example/b", hour=7), "B", "b2", "medium")])[0]
    ranked = NewsRanker(total_max_items=3, max_items_per_company=1).rank([multi, RankedNewsItem.from_external_event(b_event)])
    assert ranked == [multi]


@pytest.mark.parametrize(
    ("materialities", "expected"),
    [(("low", "low"), "low"), (("low", "medium"), "medium"), (("medium", "high"), "high")],
)
def test_materiality_aggregation_policy(materialities: tuple[str, str], expected: str) -> None:
    shared = article("Platform exact event", "https://publisher.example/exact")
    event = RouteBEventClusterer().cluster([
        judged(shared, "A", "a", materialities[0]),
        judged(shared, "B", "b", materialities[1]),
    ])[0]
    assert event.materiality == expected
