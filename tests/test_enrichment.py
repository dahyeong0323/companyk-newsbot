from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from companyk_newsbot.enrichment import PublisherArticleEnricher, extract_article_text, google_publisher_resolution_url
from companyk_newsbot.models import Article
from companyk_newsbot.portfolio_registry import PortfolioRegistry
from companyk_newsbot.rules import RouteADetector


NOW = datetime(2026, 8, 14, tzinfo=UTC)


def registry(*, short_guard: bool = False) -> PortfolioRegistry:
    companies = [
        {
            "company_id": "company-alpha01", "display_name": "AlphaBio", "source_name": "AlphaBio",
            "legal_names": ["AlphaBio"], "former_names": [], "search_terms": ["AlphaBio"],
            "match_terms": ["AlphaBio"], "ambiguity": {},
        },
        {
            "company_id": "company-beta002", "display_name": "BetaTech", "source_name": "BetaTech",
            "legal_names": ["BetaTech"], "former_names": [], "search_terms": ["BetaTech"],
            "match_terms": ["BetaTech"], "ambiguity": {},
        },
    ]
    if short_guard:
        companies.append({
            "company_id": "company-ai00003", "display_name": "AI Holdings", "source_name": "AI Holdings",
            "legal_names": ["AI Holdings"], "former_names": [], "search_terms": ["AI"],
            "match_terms": ["AI"],
            "ambiguity": {"english_required_context_for_short_form": {"AI": ["software"]}},
        })
    return PortfolioRegistry.model_validate({
        "schema_version": "1", "source": {"workbook": "fixture.xlsx", "sheet": "S", "column": "A",
        "source_sha256": "0" * 64, "generated_at": "2026-08-14", "company_count": len(companies)},
        "companies": companies,
    })


def article(query: str, *, title: str = "Unrelated headline", url: str = "https://news.google.com/articles/story") -> Article:
    return Article(
        source="Google News", source_type="google_news_rss", title=title, url=url, canonical_url=url,
        description="A concise publisher deck without the searched identity.", retrieved_at=NOW,
        origin_metadata={"query": query, "origin_queries": [query]},
    )


@pytest.mark.parametrize(
    ("html", "source", "needle"),
    [
        ("<html><head><meta property='og:title' content='AlphaBio funding'><meta property='og:description' content='투자 유치 발표'></head></html>", "opengraph", "투자 유치"),
        ("<script type='application/ld+json'>{\"@type\":\"Article\",\"headline\":\"AlphaBio\",\"articleBody\":\"AlphaBio raised Series A funding.\"}</script>", "jsonld", "Series A"),
        ("<script type='application/ld+json'>{\"@type\":\"NewsArticle\",\"headline\":\"알파바이오\",\"description\":\"임상 결과 발표\",\"articleBody\":\"본문 내용입니다.\"}</script>", "jsonld", "임상 결과"),
        ("<article><h1>AlphaBio &amp; Partner</h1><script>AlphaBio fake</script><style>.x{}</style><p>Signed a major contract.</p></article>", "article_tag", "AlphaBio & Partner"),
        ("<main><h1>AlphaBio launches</h1><noscript>noise</noscript><p>새 제품을 출시했습니다.</p></main>", "main_tag", "새 제품"),
    ],
)
def test_deterministic_html_extractors(html: str, source: str, needle: str) -> None:
    extracted = extract_article_text(html)
    assert extracted is not None
    assert extracted.source == source
    assert needle in extracted.text
    assert "fake" not in extracted.text and ".x{}" not in extracted.text and "noise" not in extracted.text


def run_enrichment(handler, values: list[Article], *, source_registry: PortfolioRegistry | None = None):
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        enricher = PublisherArticleEnricher(client=client, concurrency=2, per_host_concurrency=1, timeout_seconds=0.05)
        try:
            return await enricher.enrich_all(values, source_registry or registry())
        finally:
            await client.aclose()
    return asyncio.run(run())


def test_rss_visible_identity_skips_enrichment() -> None:
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)
    result = run_enrichment(handler, [article("AlphaBio", title="AlphaBio raises funding")])
    assert calls == 0
    assert result.metrics.enrichment_skipped_identity_already_visible == 1
    assert result.metrics.route_a_matches_from_rss == 1
    assert result.articles[0].origin_metadata["enrichment_status"] == "not_needed"


@pytest.mark.parametrize("container", ["jsonld", "article"])
def test_identity_absent_from_rss_matches_after_structured_enrichment(container: str) -> None:
    body = (
        '<script type="application/ld+json">{"@type":"NewsArticle","headline":"Funding",'
        '"articleBody":"AlphaBio completed a material financing round."}</script>'
        if container == "jsonld" else
        "<article><h1>Funding completed</h1><p>AlphaBio completed a material financing round.</p></article>"
    )
    async def handler(request):
        return httpx.Response(200, text=body, headers={"content-type": "text/html"}, request=request)
    result = run_enrichment(handler, [article("AlphaBio", url="https://publisher.example/story")])
    matches = RouteADetector(registry()).detect_scoped(result.articles[0])
    assert [match.company for match in matches] == ["AlphaBio"]
    assert result.metrics.enrichment_attempted == result.metrics.enrichment_success == 1
    assert result.metrics.route_a_matches_from_enriched_content == 1


def test_company_only_in_related_news_or_footer_never_matches() -> None:
    html = """<html><head><title>Market update</title><meta name="description" content="A general market update with no named company."></head>
    <main><h1>Sector news</h1><p>The actual article discusses broad market conditions.</p>
    <section class="related-news">Related: AlphaBio raises funding</section></main>
    <aside>Related: AlphaBio raises funding</aside><footer>AlphaBio portfolio tag</footer></html>"""
    async def handler(request):
        return httpx.Response(200, text=html, headers={"content-type": "text/html"}, request=request)
    result = run_enrichment(handler, [article("AlphaBio", url="https://publisher.example/market")])
    assert RouteADetector(registry()).detect_scoped(result.articles[0]) == []
    assert "AlphaBio" not in (result.articles[0].text or "")


@pytest.mark.parametrize(("failure", "expected"), [("timeout", "timeout"), ("403", "blocked"), ("non_html", "parse_error")])
def test_failed_enrichment_is_fail_closed(failure: str, expected: str) -> None:
    async def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if failure == "403":
            return httpx.Response(403, request=request)
        return httpx.Response(200, content=b"binary", headers={"content-type": "application/octet-stream"}, request=request)
    result = run_enrichment(handler, [article("AlphaBio", url="https://publisher.example/failure")])
    enriched = result.articles[0]
    assert enriched.origin_metadata["enrichment_status"] == expected
    assert enriched.text is None
    assert RouteADetector(registry()).detect_scoped(enriched) == []


def test_similar_substring_and_query_provenance_do_not_accept_wrong_entity() -> None:
    html = "<article><h1>BetaTech launch</h1><p>BetaTechnology announced a product update.</p></article>"
    async def handler(request):
        return httpx.Response(200, text=html, headers={"content-type": "text/html"}, request=request)
    result = run_enrichment(handler, [article("AlphaBio", url="https://publisher.example/wrong")])
    assert RouteADetector(registry()).detect_scoped(result.articles[0]) == []


def test_short_english_ambiguity_guard_applies_to_enriched_text() -> None:
    html = "<article><h1>AI award</h1><p>AI won an event award with no product context.</p></article>"
    async def handler(request):
        return httpx.Response(200, text=html, headers={"content-type": "text/html"}, request=request)
    guarded = registry(short_guard=True)
    result = run_enrichment(handler, [article("AI", url="https://publisher.example/ai")], source_registry=guarded)
    assert RouteADetector(guarded).detect_scoped(result.articles[0]) == []


def test_duplicate_url_uses_one_same_run_fetch_and_preserves_each_query_scope() -> None:
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="<article><p>AlphaBio and BetaTech announced a partnership.</p></article>",
                              headers={"content-type": "text/html"}, request=request)
    shared = "https://publisher.example/shared"
    result = run_enrichment(handler, [article("AlphaBio", url=shared), article("BetaTech", url=shared)])
    assert calls == 1
    assert result.metrics.enrichment_attempted == 1
    assert result.metrics.same_run_enrichment_cache_hits == 1
    detector = RouteADetector(registry())
    assert [m.company for m in detector.detect_scoped(result.articles[0])] == ["AlphaBio"]
    assert [m.company for m in detector.detect_scoped(result.articles[1])] == ["BetaTech"]


def test_redirect_resolution_preserves_google_url_and_records_publisher_url() -> None:
    google_url = "https://news.google.com/articles/story"
    publisher_url = "https://publisher.example/story?id=7&utm_source=google"
    async def handler(request):
        if request.url.host == "news.google.com":
            return httpx.Response(302, headers={"location": publisher_url}, request=request)
        return httpx.Response(200, text="<article><p>AlphaBio completed financing.</p></article>",
                              headers={"content-type": "text/html"}, request=request)
    result = run_enrichment(handler, [article("AlphaBio", url=google_url)])
    enriched = result.articles[0]
    assert enriched.url == google_url
    assert enriched.canonical_url == "https://publisher.example/story?id=7"
    assert enriched.origin_metadata["resolved_url"] == "https://publisher.example/story?id=7"
    assert result.metrics.resolved_publisher_urls == 1


def test_google_rss_wrapper_uses_responsive_article_resolution_route() -> None:
    value = google_publisher_resolution_url("https://news.google.com/rss/articles/token?oc=5")
    assert value.startswith("https://news.google.com/articles/token?")
    assert "hl=en-US" in value and "gl=US" in value and "ceid=US%3Aen" in value


def test_google_wrapper_html_can_resolve_a_safe_external_publisher_link() -> None:
    async def handler(request):
        if request.url.host == "news.google.com":
            return httpx.Response(200, text="<html><a href='https://publisher.example/story'>Publisher</a></html>",
                                  headers={"content-type": "text/html"}, request=request)
        return httpx.Response(200, text="<article><p>AlphaBio announced financing.</p></article>",
                              headers={"content-type": "text/html"}, request=request)
    result = run_enrichment(handler, [article("AlphaBio")])
    assert result.articles[0].canonical_url == "https://publisher.example/story"
    assert RouteADetector(registry()).detect_scoped(result.articles[0])


def test_google_decoder_attributes_resolve_publisher_before_article_fetch() -> None:
    calls = []
    google_html = "<div data-n-a-id='token' data-n-a-ts='1786680630' data-n-a-sg='signature'></div>"
    decoder = ")]}'\n\n123\n[[\"wrb.fr\",\"Fbv4je\",\"[\\\"garturlres\\\",\\\"https://publisher.example/decoded\\\"]\",null,null,null,\"generic\"]]"
    async def handler(request):
        calls.append((request.method, str(request.url)))
        if request.method == "POST":
            return httpx.Response(200, text=decoder, request=request)
        if request.url.host == "news.google.com":
            return httpx.Response(200, text=google_html, headers={"content-type": "text/html"}, request=request)
        return httpx.Response(200, text="<article><p>AlphaBio completed financing.</p></article>",
                              headers={"content-type": "text/html"}, request=request)
    result = run_enrichment(handler, [article("AlphaBio")])
    assert [method for method, _ in calls] == ["GET", "POST", "GET"]
    assert result.articles[0].canonical_url == "https://publisher.example/decoded"
    assert RouteADetector(registry()).detect_scoped(result.articles[0])


def test_redirect_to_private_network_is_blocked_without_following() -> None:
    calls = []
    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"}, request=request)
    result = run_enrichment(handler, [article("AlphaBio")])
    assert len(calls) == 1
    assert result.articles[0].origin_metadata["enrichment_status"] == "blocked"
