from __future__ import annotations

from datetime import UTC, date, datetime

from companyk_newsbot.config import KeywordMapConfig
from companyk_newsbot.judges import JudgeOutput, JudgedRouteBCandidate, SummaryOutput
from companyk_newsbot.models import Article
from companyk_newsbot.pipeline import NewsPipeline
from companyk_newsbot.rules import ExposureRegistry, RouteADetector, RouteBCandidateGenerator


def config() -> KeywordMapConfig:
    return KeywordMapConfig.model_validate({"schema_version": "test", "name": "test", "external_impact_logic": {"event_families": {"platform_infrastructure_dependency": "platform"}, "matching_rules": {"platform_infrastructure_dependency": {}}, "query_registry": {}, "causal_judge": {}}, "company_rules": {"Direct Co": {"aliases": ["Direct"], "no_justified_external_exposure": {"status": True, "reason": "test", "review_date": "2026-01-01"}}, "External Co": {"aliases": [], "external_exposures": [{"exposure_id": "play_fee", "type": "platform", "subject": {"canonical": "Google Play fees", "query_terms": ["Google Play fee"]}, "valid_from": "2024-01-01", "evidence": {"source_type": "official", "url": "https://example.com/evidence"}, "allowed_event_families": ["platform_infrastructure_dependency"], "required_event_context": ["fee"], "likely_impact_mechanisms": ["margin_pressure"]}]}}})


def article(title: str, url: str, query: str, hour: int = 8) -> Article:
    return Article(source="fixture", source_type="fixture", title=title, url=url, canonical_url=url, description="Fixture description", published_at=datetime(2026, 8, 11, hour, tzinfo=UTC), retrieved_at=datetime(2026, 8, 12, tzinfo=UTC), origin_metadata={"query": query})


def test_pipeline_runs_article_to_html_with_direct_dedup_and_external_impact() -> None:
    value = config()
    def judge(candidate):
        return JudgedRouteBCandidate(candidate, JudgeOutput(qualifies=True, company="External Co", exposure_id="play_fee", event_family="platform_infrastructure_dependency", materiality="high", impact_direction="negative", causal_mechanism="Fee increase affects margin.", rejection_reason="none"), "test", "test")
    def summarize(item):
        return SummaryOutput(summary=f"{item.company} summary.", why_it_matters="Margin pressure." if item.route == "external" else None)
    pipeline = NewsPipeline(route_a_detector=RouteADetector(value), route_b_generator=RouteBCandidateGenerator(ExposureRegistry(value)), route_b_judge=judge, summarize=summarize)
    result = pipeline.run([article("Direct Co raises funding", "https://example.com/direct", "Direct Co"), article("Direct Co raises funding", "https://example.com/direct", "Direct Co", 9), article("Google announces Play fee increase", "https://example.com/external", "Google Play fee")], report_date=date(2026, 8, 12))
    assert len(result.article_dedup.articles) == 2
    assert result.route_a_event_clusters == 1
    assert result.route_b_candidates == result.route_b_accepted == 1
    assert [item.route for item in result.ranked_items] == ["external", "direct"]
    assert "왜 이 회사에 중요한가:" in result.rendered_email.html
