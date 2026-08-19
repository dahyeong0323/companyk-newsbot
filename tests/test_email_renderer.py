from __future__ import annotations

from datetime import UTC, date, datetime

from companyk_newsbot.email import EmailNewsItem, HtmlEmailRenderer
from companyk_newsbot.dedup import article_id
from companyk_newsbot.judges import JudgeOutput, JudgedRouteBCandidate, SummaryOutput
from companyk_newsbot.models import Article
from companyk_newsbot.ranking import RankedNewsItem
from companyk_newsbot.rules import RouteAMatch, RouteBCandidate


def article(title: str) -> Article:
    return Article(source="test", source_type="fixture", title=title, url="https://example.com/?a=1&b=2", canonical_url="https://example.com/?a=1&b=2", retrieved_at=datetime(2026, 8, 12, tzinfo=UTC))


def direct_item() -> EmailNewsItem:
    match = RouteAMatch("Direct <Co>", ("Direct",), article("Funding <announced>"))
    return EmailNewsItem(RankedNewsItem.from_direct(match), SummaryOutput(fact_summary="투자 유치 소식입니다.", insight_one_liner="자금 집행 속도가 다음 확인 변수다.", insight_dimension="financing_runway", insight_mode="watchpoint", confidence="medium", evidence_article_ids=[article_id(match.article)]))


def external_item() -> EmailNewsItem:
    candidate = RouteBCandidate(article("Platform fee change"), "External Co", "internal-id", "Platform billing", ("platform_infrastructure_dependency",))
    decision = JudgeOutput(qualifies=True, company="External Co", exposure_id="internal-id", event_family="platform_infrastructure_dependency", materiality="high", impact_direction="negative", causal_mechanism="Cost pressure", rejection_reason="none")
    ranked = RankedNewsItem.from_external(JudgedRouteBCandidate(candidate, decision, "test", "test-model"))
    return EmailNewsItem(ranked, SummaryOutput(fact_summary="플랫폼 수수료 변경이 발표됐습니다.", why_it_matters="수수료 부담이 수익성에 영향을 줄 수 있습니다.", insight_one_liner="단가 전가 여부가 다음 수익성 변수다.", insight_dimension="cost_supply", insight_mode="watchpoint", confidence="medium", evidence_article_ids=[article_id(candidate.article)]))


def test_renderer_separates_routes_and_escapes_untrusted_content() -> None:
    rendered = HtmlEmailRenderer().render([direct_item(), external_item()], report_date=date(2026, 8, 12))
    assert rendered.subject == "[Company K] 포트폴리오 데일리 뉴스 | 2026-08-12 | 주요 뉴스 2건"
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
    assert 'alt="Company K Partners"' in rendered.html
    assert "data:image/png;base64," in rendered.html
    assert "background:#15213b" in rendered.html
    assert 'width="250" height="35"' in rendered.html
    assert "line-height:35px" in rendered.html


def test_route_a_zero_news_uses_the_frozen_v1_sentence() -> None:
    rendered = HtmlEmailRenderer().render([], report_date=date(2026, 8, 12), route_b_enabled=False)
    assert "오늘 컴퍼니케이파트너스 포트폴리오 회사의 주요 기사는 없습니다." in rendered.html


def test_route_a_only_renderer_omits_route_b_section_entirely() -> None:
    rendered = HtmlEmailRenderer().render(
        [direct_item()], report_date=date(2026, 8, 12), route_b_enabled=False
    )
    assert "2. 포트폴리오 영향 뉴스" not in rendered.html
    assert "해당 회사에 중요한 이유:" not in rendered.html


def test_renderer_groups_same_company_events_once_and_sorts_them_newest_first() -> None:
    older_article = Article(
        source="test", source_type="fixture", title="Older event", url="https://example.com/older",
        canonical_url="https://example.com/older", retrieved_at=datetime(2026, 8, 12, tzinfo=UTC),
        published_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    newer_article = Article(
        source="test", source_type="fixture", title="Newer event", url="https://example.com/newer",
        canonical_url="https://example.com/newer", retrieved_at=datetime(2026, 8, 12, tzinfo=UTC),
        published_at=datetime(2026, 8, 11, tzinfo=UTC),
    )
    other_article = Article(
        source="test", source_type="fixture", title="Other company event", url="https://example.com/other",
        canonical_url="https://example.com/other", retrieved_at=datetime(2026, 8, 12, tzinfo=UTC),
        published_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    summary = lambda item: SummaryOutput(
        fact_summary="요약", insight_one_liner="투자자 관점", insight_dimension="financing_runway",
        insight_mode="watchpoint", confidence="medium", evidence_article_ids=[article_id(item.article)],
    )
    older = RouteAMatch("Alpha", ("Alpha",), older_article)
    newer = RouteAMatch("Alpha", ("Alpha",), newer_article)
    other = RouteAMatch("Beta", ("Beta",), other_article)
    rendered = HtmlEmailRenderer().render(
        [
            EmailNewsItem(RankedNewsItem.from_direct(older), summary(older)),
            EmailNewsItem(RankedNewsItem.from_direct(other), summary(other)),
            EmailNewsItem(RankedNewsItem.from_direct(newer), summary(newer)),
        ],
        report_date=date(2026, 8, 12), route_b_enabled=False,
    )
    assert rendered.html.count('data-company-group="Alpha"') == 1
    assert rendered.html.index('data-company-group="Alpha"') < rendered.html.index('data-company-group="Beta"')
    assert rendered.html.index("Newer event") < rendered.html.index("Older event") < rendered.html.index('data-company-group="Beta"')
    assert 'data-published-at="2026-08-11T00:00:00+00:00"' in rendered.html
    assert 'align="right" valign="bottom" style="color:#697386">' in rendered.html
    assert "float:right" not in rendered.html
    assert "회사: Alpha" not in rendered.html
    assert ">Alpha</div>" in rendered.html
