from __future__ import annotations

from datetime import UTC, datetime

from companyk_newsbot.config import KeywordMapConfig
from companyk_newsbot.models import Article
from companyk_newsbot.rules import RouteADetector


def article(title: str, description: str | None = None) -> Article:
    return Article(
        source="test",
        source_type="fixture",
        title=title,
        url="https://example.com/news",
        canonical_url="https://example.com/news",
        description=description,
        retrieved_at=datetime(2026, 8, 12, tzinfo=UTC),
    )


def config() -> KeywordMapConfig:
    return KeywordMapConfig.model_validate(
        {
            "schema_version": "test",
            "name": "test-map",
            "external_impact_logic": {
                "event_families": {"policy_regulatory": "policy"},
                "matching_rules": {"policy_regulatory": {}},
                "query_registry": {},
                "causal_judge": {},
            },
            "company_rules": {
                "Alpha Ventures": {
                    "aliases": ["Alpha", "ALPHA-X"],
                    "no_justified_external_exposure": {"status": True, "reason": "test", "review_date": "2026-01-01"},
                    "negative_terms": ["Alphabet"],
                },
                "Travel Co": {
                    "aliases": ["Voyage"],
                    "no_justified_external_exposure": {"status": True, "reason": "test", "review_date": "2026-01-01"},
                    "forbidden_standalone": ["Voyage"],
                    "required_context_for_forbidden": ["booking", "travel"],
                },
                "Korean Company": {
                    "aliases": ["가이아"],
                    "no_justified_external_exposure": {"status": True, "reason": "test", "review_date": "2026-01-01"},
                    "required_context": ["바이오", "치료제"],
                },
                "Port One": {
                    "aliases": ["PortOne"],
                    "no_justified_external_exposure": {"status": True, "reason": "test", "review_date": "2026-01-01"},
                    "english_negative_context": ["shipping", "freight"],
                },
            },
        }
    )


def test_matches_registered_company_name_and_alias() -> None:
    matches = RouteADetector(config()).detect(article("ALPHA-X announces new financing"))
    assert [(match.company, match.matched_terms) for match in matches] == [("Alpha Ventures", ("ALPHA-X",))]


def test_rejects_ascii_substring_and_negative_entity_only_match() -> None:
    detector = RouteADetector(config())
    assert detector.detect(article("Alphabet reports earnings")) == []
    assert detector.detect(article("The alphaX prototype ships")) == []


def test_forbidden_standalone_requires_registered_context() -> None:
    detector = RouteADetector(config())
    assert detector.detect(article("Voyage wins an award")) == []
    assert [match.company for match in detector.detect(article("Voyage travel booking grows"))] == ["Travel Co"]


def test_required_context_and_english_ambiguity_guard() -> None:
    detector = RouteADetector(config())
    assert detector.detect(article("가이아 closes financing")) == []
    assert [match.company for match in detector.detect(article("가이아 바이오 치료제 financing"))] == ["Korean Company"]
    assert detector.detect(article("PortOne expands freight shipping service")) == []
