from __future__ import annotations

from datetime import UTC, date, datetime

from companyk_newsbot.email import EmailNewsItem, HtmlEmailRenderer
from companyk_newsbot.judges import JudgeOutput, JudgedRouteBCandidate, SummaryOutput
from companyk_newsbot.models import Article
from companyk_newsbot.ranking import RankedNewsItem
from companyk_newsbot.rules import RouteAMatch, RouteBCandidate


def article(title: str) -> Article:
    return Article(source="test", source_type="fixture", title=title, url="https://example.com/?a=1&b=2", canonical_url="https://example.com/?a=1&b=2", retrieved_at=datetime(2026, 8, 12, tzinfo=UTC))


def direct_item() -> EmailNewsItem:
    match = RouteAMatch("Direct <Co>", ("Direct",), article("Funding <announced>"))
    return EmailNewsItem(RankedNewsItem.from_direct(match), SummaryOutput(summary="투자 유치 소식입니다."))


def external_item() -> EmailNewsItem:
    candidate = RouteBCandidate(article("Platform fee change"), "External Co", "internal-id", "Platform billing", ("platform_infrastructure_dependency",))
    decision = JudgeOutput(qualifies=True, company="External Co", exposure_id="internal-id", event_family="platform_infrastructure_dependency", materiality="high", impact_direction="negative", causal_mechanism="Cost pressure", rejection_reason="none")
    ranked = RankedNewsItem.from_external(JudgedRouteBCandidate(candidate, decision, "test", "test-model"))
    return EmailNewsItem(ranked, SummaryOutput(summary="플랫폼 수수료 변경이 발표됐습니다.", why_it_matters="수수료 부담이 수익성에 영향을 줄 수 있습니다."))


def test_renderer_separates_routes_and_escapes_untrusted_content() -> None:
    rendered = HtmlEmailRenderer().render([direct_item(), external_item()], report_date=date(2026, 8, 12))
    assert rendered.subject == "[Company K] 포트폴리오 데일리 뉴스 | 2026-08-12"
    assert "1. 기업 직접 뉴스" in rendered.html
    assert "2. 포트폴리오 영향 뉴스" in rendered.html
    assert "왜 이 회사에 중요한가:" in rendered.html
    assert "Direct &lt;Co&gt;" in rendered.html
    assert "Funding &lt;announced&gt;" in rendered.html
    assert "internal-id" not in rendered.html
    assert "https://example.com/?a=1&amp;b=2" in rendered.html


def test_renderer_displays_empty_section_without_error() -> None:
    rendered = HtmlEmailRenderer().render([direct_item()], report_date=date(2026, 8, 12))
    assert "해당 뉴스가 없습니다." in rendered.html
