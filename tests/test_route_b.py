from __future__ import annotations

from datetime import UTC, datetime

from companyk_newsbot.config import KeywordMapConfig
from companyk_newsbot.models import Article
from companyk_newsbot.rules import ExposureRegistry, RouteBCandidateGenerator


def config() -> KeywordMapConfig:
    exposure = lambda identifier, company: {
        "exposure_id": identifier,
        "type": "platform_dependency",
        "subject": {"canonical": "Google Play billing", "query_terms": ["Google Play billing", "Play fees"]},
        "valid_from": "2024-01-01",
        "evidence": {"source_type": "official", "url": f"https://example.com/{company}"},
        "allowed_event_families": ["platform_infrastructure_dependency"],
        "required_event_context": ["fee"],
        "likely_impact_mechanisms": ["margin_pressure"],
    }
    return KeywordMapConfig.model_validate(
        {
            "schema_version": "test",
            "name": "test-map",
            "external_impact_logic": {
                "event_families": {"platform_infrastructure_dependency": "platform"},
                "matching_rules": {"platform_infrastructure_dependency": {}},
                "query_registry": {},
                "causal_judge": {},
            },
            "company_rules": {
                "Company A": {"aliases": [], "external_exposures": [exposure("company_a_play", "a")]},
                "Company B": {"aliases": [], "external_exposures": [exposure("company_b_play", "b")]},
            },
        }
    )


def article(query: str | None, published_at: datetime | None = datetime(2025, 1, 1, tzinfo=UTC)) -> Article:
    metadata = {} if query is None else {"query": query}
    return Article(
        source="Google News",
        source_type="google_news_rss",
        title="Platform announces a billing change",
        url="https://example.com/article",
        canonical_url="https://example.com/article",
        published_at=published_at,
        retrieved_at=datetime(2026, 8, 12, tzinfo=UTC),
        origin_metadata=metadata,
    )


def test_registry_deduplicates_shared_queries_and_retains_all_impact_links() -> None:
    registry = ExposureRegistry(config())

    query = registry.lookup("google play billing")

    assert query is not None
    assert query.query == "Google Play billing"
    assert {link.exposure_id for link in query.links} == {"company_a_play", "company_b_play"}
    assert len(registry.queries) == 2


def test_candidate_generator_only_uses_registered_collector_queries() -> None:
    result = RouteBCandidateGenerator(ExposureRegistry(config())).generate([article("Google Play billing")])

    assert {candidate.company for candidate in result.candidates} == {"Company A", "Company B"}
    assert result.rejections == ()


def test_candidate_generator_rejects_unregistered_or_missing_query() -> None:
    result = RouteBCandidateGenerator(ExposureRegistry(config())).generate([article("generic AI news"), article(None)])

    assert result.candidates == ()
    assert [rejection.reason for rejection in result.rejections] == ["unregistered_query", "missing_registered_exposure_query"]


def test_candidate_generator_enforces_knowledge_time_guard() -> None:
    result = RouteBCandidateGenerator(ExposureRegistry(config())).generate(
        [article("Play fees", datetime(2023, 12, 31, tzinfo=UTC)), article("Play fees", None)]
    )

    assert result.candidates == ()
    assert [rejection.reason for rejection in result.rejections] == [
        "before_exposure_valid_from",
        "before_exposure_valid_from",
        "missing_published_at",
    ]
