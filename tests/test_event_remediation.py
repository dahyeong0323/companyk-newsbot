from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from companyk_newsbot.dedup import (
    EventResolverOutput,
    LunaEventPairResolver,
    ResolverResult,
    RouteAEventClusterer,
    RouteBEventClusterer,
)
from companyk_newsbot.judges import JudgeOutput, JudgedRouteBCandidate
from companyk_newsbot.models import Article
from companyk_newsbot.rules import RouteAMatch, RouteBCandidate


NOW = datetime(2026, 8, 11, 8, tzinfo=UTC)


def article(title: str, url: str, *, hour: int = 8, source: str = "publisher", description: str | None = None, text: str | None = None) -> Article:
    return Article(
        source=source,
        source_type="fixture",
        title=title,
        url=url,
        canonical_url=url,
        published_at=NOW.replace(hour=hour),
        retrieved_at=datetime(2026, 8, 12, tzinfo=UTC),
        description=description or title,
        text=text,
    )


def direct(value: Article, company: str = "Acme") -> RouteAMatch:
    return RouteAMatch(company, (company,), value)


def judged(
    value: Article,
    *,
    company: str = "Acme",
    exposure: str = "exposure",
    family: str = "competition",
    materiality: str = "medium",
) -> JudgedRouteBCandidate:
    candidate = RouteBCandidate(value, company, exposure, "External subject", (family,))
    decision = JudgeOutput(
        qualifies=True,
        company=company,
        exposure_id=exposure,
        event_family=family,
        materiality=materiality,
        impact_direction="mixed",
        causal_mechanism="Approved causal mechanism.",
        rejection_reason="none",
    )
    return JudgedRouteBCandidate(candidate, decision, "test", "test")


class FixedResolver:
    def __init__(self, decision: str = "SAME_EVENT", failure_type: str | None = None) -> None:
        self.decision = decision
        self.failure_type = failure_type
        self.calls = 0

    def resolve(self, left: Article, right: Article) -> ResolverResult:
        self.calls += 1
        return ResolverResult(self.decision, "fixture decision", True, self.failure_type)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("left", "right", "reason"),
    [
        ("Acme partnership with Alpha", "Acme partnership with Beta", "counterparty_conflict"),
        ("Acme acquires Alpha", "Acme partners with Alpha", "action_conflict"),
        ("Acme filing Alpha", "Acme closing Alpha", "milestone_conflict"),
        ("Acme partnership with Alpha on 2026-08-10", "Acme partnership with Alpha on 2026-08-11", "explicit_date_conflict"),
        ("Acme raises $10 million from Alpha", "Acme raises $20 million from Alpha", "amount_conflict"),
        ("Acme acquires 10% of Alpha", "Acme acquires 20% of Alpha", "percentage_conflict"),
    ],
)
def test_route_a_distinctive_anchor_conflicts_keep_events_separate(left: str, right: str, reason: str) -> None:
    clusterer = RouteAEventClusterer(resolver=FixedResolver())
    events = clusterer.cluster([direct(article(left, "https://one.example/a")), direct(article(right, "https://two.example/b", hour=9))])
    assert len(events) == 2
    assert clusterer.metrics.deterministic_different_event == 1


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Acme partnership with Alpha", "Acme partnership with Beta"),
        ("Acme acquires Alpha", "Acme partners with Alpha"),
        ("Acme filing Alpha", "Acme closing Alpha"),
        ("Acme partnership with Alpha on 2026-08-10", "Acme partnership with Alpha on 2026-08-11"),
        ("Acme raises $10 million from Alpha", "Acme raises $20 million from Alpha"),
    ],
)
def test_route_b_distinctive_anchor_conflicts_keep_events_separate(left: str, right: str) -> None:
    clusterer = RouteBEventClusterer(resolver=FixedResolver())
    events = clusterer.cluster([judged(article(left, "https://one.example/a")), judged(article(right, "https://two.example/b", hour=9))])
    assert len(events) == 2
    assert clusterer.metrics.deterministic_different_event == 1


@pytest.mark.parametrize("route", ["a", "b"])
def test_absent_anchor_is_not_a_conflict_and_luna_can_merge(route: str) -> None:
    resolver = FixedResolver("SAME_EVENT")
    values = [
        article("Acme partnership with Alpha on 2026-08-10", "https://one.example/a"),
        article("Acme partnership update", "https://two.example/b", hour=9),
    ]
    events = (
        RouteAEventClusterer(resolver=resolver).cluster([direct(value) for value in values])
        if route == "a"
        else RouteBEventClusterer(resolver=resolver).cluster([judged(value) for value in values])
    )
    assert len(events) == 1
    assert resolver.calls == 1


@pytest.mark.parametrize("route", ["a", "b"])
def test_generic_overlap_is_ambiguous_not_same(route: str) -> None:
    left = article("Acme platform market update", "https://one.example/a")
    right = article("Acme platform market outlook", "https://two.example/b", hour=9)
    clusterer = RouteAEventClusterer() if route == "a" else RouteBEventClusterer()
    events = clusterer.cluster([direct(left), direct(right)] if route == "a" else [judged(left), judged(right)])
    assert len(events) == 2
    assert clusterer.metrics.ambiguous_pairs == 1


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Acme raises $10 million from Alpha on 2026-08-10", "Acme funding from Alpha totals $10 million on 2026-08-10"),
        ("Acme IPO filing submitted on 2026-08-10", "Acme files for IPO on 2026-08-10"),
        ("Acme partnership with Alpha on 2026-08-10", "Acme agreement with Alpha dated 2026-08-10"),
    ],
)
def test_strong_shared_event_anchors_merge_without_luna(left: str, right: str) -> None:
    clusterer = RouteAEventClusterer()
    events = clusterer.cluster([direct(article(left, "https://one.example/a")), direct(article(right, "https://two.example/b", hour=9))])
    if "agreement" in right:
        assert len(events) == 2
        assert clusterer.metrics.deterministic_same_event == 0
    else:
        assert len(events) == 1
        assert clusterer.metrics.deterministic_same_event == 1


def test_same_event_with_omitted_amount_merges_only_after_luna() -> None:
    resolver = FixedResolver("SAME_EVENT")
    events = RouteAEventClusterer(resolver=resolver).cluster([
        direct(article("Acme raises funding", "https://one.example/a")),
        direct(article("Acme raises $10 million", "https://two.example/b", hour=9)),
    ])
    assert len(events) == 1
    assert resolver.calls == 1


@pytest.mark.parametrize("route", ["a", "b"])
def test_all_member_pair_check_prevents_transitive_false_merge(route: str) -> None:
    values = [
        article("Acme partnership with Alpha", "https://one.example/a"),
        article("Acme partnership update", "https://two.example/b", hour=9),
        article("Acme partnership with Beta", "https://three.example/c", hour=10),
    ]
    memberships = []
    for ordered_values in (values, list(reversed(values))):
        resolver = FixedResolver("SAME_EVENT")
        events = (
            RouteAEventClusterer(resolver=resolver).cluster([direct(value) for value in ordered_values])
            if route == "a"
            else RouteBEventClusterer(resolver=resolver).cluster([judged(value) for value in ordered_values])
        )
        memberships.append(
            sorted(
                sorted(
                    article.url
                    for article in (
                        (match.article for match in event.all_matches)
                        if route == "a"
                        else event.all_articles
                    )
                )
                for event in events
            )
        )
        assert sorted(event.coverage_count for event in events) == [1, 2]
    assert memberships[0] == memberships[1]


@pytest.mark.parametrize(("decision", "expected"), [("SAME_EVENT", 1), ("DIFFERENT_EVENT", 2)])
def test_ambiguous_luna_decision_controls_both_route_clusterers(decision: str, expected: int) -> None:
    resolver = FixedResolver(decision)
    values = [article("Acme market update", "https://one.example/a"), article("Acme market outlook", "https://two.example/b", hour=9)]
    assert len(RouteAEventClusterer(resolver=resolver).cluster([direct(value) for value in values])) == expected
    assert len(RouteBEventClusterer(resolver=resolver).cluster([judged(value) for value in values])) == expected


class FakeResponses:
    def __init__(self, parsed: object = None, error: Exception | None = None) -> None:
        self.parsed, self.error = parsed, error

    def parse(self, **kwargs: object) -> object:
        if self.error:
            raise self.error
        return SimpleNamespace(output_parsed=self.parsed)


@pytest.mark.parametrize(
    ("responses", "failure"),
    [
        (FakeResponses(error=TimeoutError("late")), "timeout"),
        (FakeResponses(parsed=None), "schema_failure"),
        (FakeResponses(error=RuntimeError("client")), "client_error"),
    ],
)
def test_luna_resolver_failures_are_audited_and_keep_events_separate(responses: FakeResponses, failure: str) -> None:
    resolver = LunaEventPairResolver(SimpleNamespace(responses=responses))
    clusterer = RouteAEventClusterer(resolver=resolver)
    values = [direct(article("Acme market update", "https://one.example/a")), direct(article("Acme market outlook", "https://two.example/b", hour=9))]
    events = clusterer.cluster(values)
    assert len(events) == 2
    assert clusterer.metrics.luna_event_dedup_failures == 1
    audit = next(decision for event in events for decision in event.dedup_decisions)
    assert audit.luna_invoked is True
    assert audit.luna_failure_type == failure


@pytest.mark.parametrize(("decision", "count"), [("SAME_EVENT", 1), ("DIFFERENT_EVENT", 2)])
def test_luna_structured_result_is_used(decision: str, count: int) -> None:
    resolver = LunaEventPairResolver(SimpleNamespace(responses=FakeResponses(EventResolverOutput(decision=decision, short_reason="grounded"))))
    values = [direct(article("Acme market update", "https://one.example/a")), direct(article("Acme market outlook", "https://two.example/b", hour=9))]
    assert len(RouteAEventClusterer(resolver=resolver).cluster(values)) == count


def test_same_url_different_exposures_and_families_is_one_external_event() -> None:
    first = judged(article("Platform fee decision", "https://publisher.example/event"), company="A", exposure="a-fee", family="competition")
    second_article = first.candidate.article.model_copy(update={"title": "Platform fee decision updated", "published_at": NOW.replace(hour=9)})
    second = judged(second_article, company="B", exposure="b-policy", family="policy_regulatory", materiality="high")
    event = RouteBEventClusterer().cluster([second, first])[0]
    assert event.coverage_count == 1
    assert event.companies == ("A", "B")
    assert len(event.impact_links) == 2
    assert event.source_families == ("competition", "policy_regulatory")
    assert event.event_family == "competition"


def test_same_normalized_title_is_stable_article_identity_even_with_different_urls() -> None:
    values = [
        judged(article("Platform fee decision", "https://feed.example/one"), exposure="one", family="competition"),
        judged(article("Platform fee decision!", "https://feed.example/two"), exposure="two", family="policy_regulatory"),
    ]
    event = RouteBEventClusterer().cluster(values)[0]
    assert event.coverage_count == 1
    assert len(event.impact_links) == 2
    assert event.source_families == ("competition", "policy_regulatory")


def test_same_company_distinct_external_events_stay_separate() -> None:
    events = RouteBEventClusterer().cluster([
        judged(article("Regulator fines platform $10 million", "https://one.example/a")),
        judged(article("Regulator fines platform $20 million", "https://two.example/b", hour=9)),
    ])
    assert len(events) == 2
