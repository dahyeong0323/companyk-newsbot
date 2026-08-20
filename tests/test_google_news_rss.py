from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from companyk_newsbot.collectors.google_news_rss import CollectionError, GoogleNewsRSSCollector, RSSCollectorSettings, canonicalize_url


RSS_BODY = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Google News</title><language>en-US</language>
<item><title>Example &amp;amp; Co raises funding</title>
<link>https://news.example.com/story?utm_source=google&amp;id=42#section</link>
<pubDate>Mon, 11 Aug 2026 08:00:00 GMT</pubDate>
<source url="https://www.reuters.com/">Reuters</source>
<description>&lt;b&gt;Funding&lt;/b&gt; &amp;amp; expansion announced.</description></item>
<item><title></title><link>https://news.example.com/invalid</link></item>
</channel></rss>"""


async def no_sleep(_: float) -> None:
    return None


def collector_for(body: bytes, status_code: int = 200) -> GoogleNewsRSSCollector:
    transport = httpx.MockTransport(lambda request: httpx.Response(status_code, content=body, request=request))
    client = httpx.AsyncClient(transport=transport)
    return GoogleNewsRSSCollector(
        client=client,
        now=lambda: datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        sleep=no_sleep,
        random_value=lambda: 0.5,
    )


def test_collects_and_normalizes_google_news_items() -> None:
    articles = asyncio.run(collector_for(RSS_BODY).collect("Example Co funding"))

    assert len(articles) == 1
    article = articles[0]
    assert article.title == "Example & Co raises funding"
    assert article.source == "Reuters"
    assert article.canonical_url == "https://news.example.com/story?id=42"
    assert article.description == "Funding & expansion announced."
    assert article.published_at == datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    assert article.origin_metadata["query"] == "Example Co funding"
    assert article.origin_metadata["origin_queries"] == ["Example Co funding"]
    assert article.origin_metadata["google_news_url"] == "https://news.example.com/story?utm_source=google&id=42#section"
    assert article.retrieved_at == datetime(2026, 8, 12, 7, 0, tzinfo=UTC)


@pytest.mark.parametrize("hint", ["when:2d", "when:7d"])
def test_runtime_freshness_hint_is_sent_without_changing_base_query_metadata(hint: str) -> None:
    observed_query = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_query
        observed_query = request.url.params["q"]
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client, freshness_hint=hint)
        try:
            return await collector.collect("Example Co")
        finally:
            await client.aclose()

    articles = asyncio.run(run())
    assert observed_query == f"Example Co {hint}"
    assert articles[0].origin_metadata["query"] == "Example Co"


def test_default_korean_edition_is_used_for_portfolio_news_queries() -> None:
    observed = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.update(request.url.params)
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def run() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client)
        try:
            await collector.collect("오아시스마켓")
        finally:
            await client.aclose()

    asyncio.run(run())
    assert observed["hl"] == "ko-KR"
    assert observed["gl"] == "KR"
    assert observed["ceid"] == "KR:ko"


def test_explicit_non_korean_edition_derives_a_matching_ceid() -> None:
    observed = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.update(request.url.params)
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def run() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client, language="en-US", country="US")
        try:
            await collector.collect("Example Co")
        finally:
            await client.aclose()

    asyncio.run(run())
    assert observed["ceid"] == "US:en"


def test_collect_rejects_blank_query() -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        asyncio.run(collector_for(RSS_BODY).collect("  "))


def test_collect_raises_for_http_failure() -> None:
    with pytest.raises(CollectionError, match="request failed"):
        asyncio.run(collector_for(b"", status_code=503).collect("Example Co"))


def test_canonicalize_url_removes_tracking_without_changing_meaningful_query() -> None:
    assert canonicalize_url("https://example.com/a?gclid=x&id=5&utm_campaign=test#top") == "https://example.com/a?id=5"


def test_collect_many_uses_bounded_concurrency() -> None:
    active = 0
    maximum = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client, settings=RSSCollectorSettings(concurrency=2))
        try:
            return await collector.collect_many([f"query {index}" for index in range(6)])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert len(result.articles) == 6
    assert len(result.successes) == 6
    assert not result.failures
    assert maximum == 2


def test_collect_many_isolates_timeout_http_and_parse_failures() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["q"]
        if query == "timeout":
            raise httpx.ReadTimeout("too slow", request=request)
        if query == "http":
            return httpx.Response(503, request=request)
        if query == "parse":
            return httpx.Response(200, content=b"not an rss feed", request=request)
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client, sleep=no_sleep, random_value=lambda: 0.5)
        try:
            return await collector.collect_many(["success", "timeout", "http", "parse"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert [item.status for item in result.queries] == ["success", "timeout", "service_unavailable", "parse_error"]
    assert len(result.articles) == 1
    assert len(result.successes) == 1
    assert len(result.failures) == 3


@pytest.mark.parametrize("failure_type", ["connect_error", "connect_timeout"])
def test_connection_failure_is_retried_once_then_succeeds(failure_type: str) -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if failure_type == "connect_timeout":
                raise httpx.ConnectTimeout("connect timed out", request=request)
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client, sleep=no_sleep)
        try:
            return await collector.collect_many(["retry succeeds"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert attempts == 2
    assert len(result.successes) == 1
    assert len(result.articles) == 1


@pytest.mark.parametrize("failure_type", ["http_404", "parse_error"])
def test_non_transient_failures_are_not_retried(failure_type: str) -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if failure_type == "http_404":
            return httpx.Response(404, request=request)
        return httpx.Response(200, content=b"not an rss feed", request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client, sleep=no_sleep)
        try:
            return await collector.collect_many(["no retry"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert attempts == 1
    assert len(result.failures) == 1


def test_per_query_hard_timeout_does_not_block_other_queries() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["q"] == "slow":
            await asyncio.sleep(0.08)
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        settings = RSSCollectorSettings(concurrency=2, per_query_deadline_seconds=0.02, collection_deadline_seconds=0.2)
        collector = GoogleNewsRSSCollector(client=client, settings=settings)
        try:
            return await collector.collect_many(["slow", "fast"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert [item.status for item in result.queries] == ["timeout", "success"]
    assert len(result.articles) == 1


def test_global_deadline_cancels_unfinished_work_and_preserves_successes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["q"] != "fast":
            await asyncio.sleep(0.2)
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        settings = RSSCollectorSettings(concurrency=2, per_query_deadline_seconds=1.0, collection_deadline_seconds=0.04)
        collector = GoogleNewsRSSCollector(client=client, settings=settings)
        try:
            return await collector.collect_many(["fast", "slow-1", "slow-2"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.queries[0].status == "success"
    assert [item.status for item in result.queries[1:]] == ["collection_deadline", "collection_deadline"]
    assert len(result.articles) == 1


def test_duplicate_normalized_queries_are_fetched_once() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client)
        try:
            return await collector.collect_many([" Example   Co ", "example co", "Ｅｘａｍｐｌｅ Ｃｏ"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert calls == 1
    assert len(result.queries) == 1
    assert len(result.articles) == 1


@pytest.mark.parametrize("failures_before_success", [1, 2])
def test_503_retries_then_succeeds(failures_before_success: int) -> None:
    attempts = 0
    delays = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= failures_before_success:
            return httpx.Response(503, request=request)
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client, sleep=sleep, random_value=lambda: 0.5)
        try:
            return await collector.collect_many(["retry"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert attempts == failures_before_success + 1
    assert result.queries[0].status == "success"
    assert result.queries[0].retry_attempts == failures_before_success
    assert delays == [0.5, 1.0][:failures_before_success]


def test_repeated_503_stops_after_two_retries() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client, sleep=no_sleep)
        try:
            return await collector.collect_many(["bounded"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert attempts == 3
    assert result.queries[0].status == "service_unavailable"
    assert result.metrics()["rss_503"] == 1
    assert result.metrics()["rss_retry_attempts"] == 2


def test_429_retry_after_is_bounded_and_recorded() -> None:
    attempts = 0
    delays = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "99"}, request=request)
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client, sleep=sleep)
        try:
            return await collector.collect_many(["limited"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert delays == [5.0]
    assert result.queries[0].retry_after_used == 1
    assert result.metrics()["rss_retry_after_used"] == 1


def test_429_without_retry_after_uses_deterministic_backoff() -> None:
    attempts = 0
    delays = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, request=request)
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client, sleep=sleep, random_value=lambda: 0.5)
        try:
            return await collector.collect_many(["limited"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.queries[0].status == "success"
    assert delays == [0.5]


@pytest.mark.parametrize("failures_before_success, expected_status", [(1, "success"), (3, "timeout")])
def test_read_timeout_retry_is_bounded(failures_before_success: int, expected_status: str) -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= failures_before_success:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client, sleep=no_sleep)
        try:
            return await collector.collect_many(["timeout"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.queries[0].status == expected_status
    assert attempts == (2 if expected_status == "success" else 3)


def test_systemic_breaker_stops_hammering_and_accounts_for_skips() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        settings = RSSCollectorSettings(max_retries=0, breaker_cooldown_seconds=0)
        collector = GoogleNewsRSSCollector(client=client, settings=settings, sleep=no_sleep)
        try:
            return await collector.collect_many([f"q{i}" for i in range(20)])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert calls == 8
    assert len(result.queries) == 20
    assert result.systemic_breaker_triggered is True
    assert result.metrics()["rss_skipped_systemic_failure"] == 12


def test_isolated_transient_failures_do_not_trip_breaker() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["q"]
        if query in {"q0", "q1"}:
            return httpx.Response(503, request=request)
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        settings = RSSCollectorSettings(max_retries=0, breaker_cooldown_seconds=0)
        collector = GoogleNewsRSSCollector(client=client, settings=settings, sleep=no_sleep)
        try:
            return await collector.collect_many([f"q{i}" for i in range(12)])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.systemic_breaker_triggered is False
    assert result.metrics()["rss_query_success"] == 10
    assert result.metrics()["rss_skipped_systemic_failure"] == 0


def test_breaker_serial_probe_recovers_and_finishes_queue() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params["q"][1:])
        if index < 6:
            return httpx.Response(503, request=request)
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        settings = RSSCollectorSettings(max_retries=0, breaker_cooldown_seconds=0)
        collector = GoogleNewsRSSCollector(client=client, settings=settings, sleep=no_sleep)
        try:
            return await collector.collect_many([f"q{i}" for i in range(12)])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.systemic_breaker_triggered is True
    assert result.metrics()["rss_query_success"] == 6
    assert result.metrics()["rss_skipped_systemic_failure"] == 0


def test_collector_sends_stable_request_headers() -> None:
    observed = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.update(request.headers)
        return httpx.Response(200, content=RSS_BODY, request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client)
        try:
            await collector.collect("headers")
        finally:
            await client.aclose()

    asyncio.run(run())
    assert observed["user-agent"].startswith("CompanyK-Newsbot/")
    assert "application/rss+xml" in observed["accept"]
    assert observed["accept-language"].startswith("ko-KR")
