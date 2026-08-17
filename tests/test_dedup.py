from __future__ import annotations

from datetime import UTC, datetime

from companyk_newsbot.dedup import ArticleDeduplicator, RouteAEventClusterer
from companyk_newsbot.models import Article
from companyk_newsbot.rules import RouteAMatch


def article(title: str, url: str, *, published_at: datetime | None = None) -> Article:
    return Article(
        source="test",
        source_type="fixture",
        title=title,
        url=url,
        canonical_url=url,
        retrieved_at=datetime(2026, 8, 12, tzinfo=UTC),
        published_at=published_at,
    )


def match(company: str, title: str, url: str, *, published_at: datetime | None = None) -> RouteAMatch:
    return RouteAMatch(company=company, matched_terms=(company,), article=article(title, url, published_at=published_at))


def test_article_dedup_prefers_first_article_and_records_canonical_url_reason() -> None:
    original = article("Alpha raises funding", "https://example.com/a")
    duplicate = article("Alpha raises funding – syndicated", "https://example.com/a")

    result = ArticleDeduplicator().deduplicate([original, duplicate])

    assert result.articles == (original,)
    assert result.duplicate_groups[0].duplicates == (duplicate,)
    assert result.duplicate_groups[0].reason == "canonical_url"


def test_article_dedup_collapses_identical_normalized_titles_from_different_feeds() -> None:
    first = article("Alpha raises funding!", "https://first.example/a")
    second = article("alpha raises funding", "https://second.example/a")

    result = ArticleDeduplicator().deduplicate([first, second])

    assert result.articles == (first,)
    assert result.duplicate_groups[0].reason == "normalized_title"


def test_article_dedup_preserves_all_query_and_company_provenance() -> None:
    first = article("Shared event", "https://example.com/shared").model_copy(update={
        "origin_metadata": {"query": "AlphaBio", "origin_queries": ["AlphaBio"], "candidate_company_ids": ["company-alpha"]}
    })
    second = article("Shared event syndicated", "https://example.com/shared").model_copy(update={
        "origin_metadata": {"query": "BetaTech", "origin_queries": ["BetaTech"], "candidate_company_ids": ["company-beta"]}
    })
    result = ArticleDeduplicator().deduplicate([first, second])
    metadata = result.articles[0].origin_metadata
    assert metadata["origin_queries"] == ["AlphaBio", "BetaTech"]
    assert metadata["candidate_company_ids"] == ["company-alpha", "company-beta"]


def test_event_cluster_groups_same_company_cross_publication_coverage() -> None:
    early = match(
        "Alpha Ventures",
        "Alpha Ventures raises $10m Series B funding",
        "https://official.example/a",
        published_at=datetime(2026, 8, 11, 8, 0, tzinfo=UTC),
    )
    later = match(
        "Alpha Ventures",
        "Alpha Ventures secures $10m in Series B financing",
        "https://press.example/a",
        published_at=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
    )

    class Same:
        def resolve(self, left, right):
            from companyk_newsbot.dedup import ResolverResult
            return ResolverResult("SAME_EVENT", "same funding fixture", True)

    clusters = RouteAEventClusterer(resolver=Same()).cluster([later, early])

    assert len(clusters) == 1
    assert clusters[0].primary == early
    assert clusters[0].coverage == (later,)
    assert clusters[0].coverage_count == 2


def test_event_cluster_keeps_conflicting_amounts_and_companies_separate() -> None:
    matches = [
        match("Alpha Ventures", "Alpha Ventures raises $10m Series B funding", "https://a.example/10"),
        match("Alpha Ventures", "Alpha Ventures raises $12m Series B funding", "https://a.example/12"),
        match("Beta Ventures", "Beta Ventures raises $10m Series B funding", "https://b.example/10"),
    ]

    clusters = RouteAEventClusterer().cluster(matches)

    assert len(clusters) == 3
