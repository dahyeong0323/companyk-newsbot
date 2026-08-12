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
<description>&lt;b&gt;Funding&lt;/b&gt; &amp;amp; expansion announced.</description></item>
<item><title></title><link>https://news.example.com/invalid</link></item>
</channel></rss>"""


def collector_for(body: bytes, status_code: int = 200) -> GoogleNewsRSSCollector:
    transport = httpx.MockTransport(lambda request: httpx.Response(status_code, content=body, request=request))
    client = httpx.AsyncClient(transport=transport)
    return GoogleNewsRSSCollector(
        client=client,
        now=lambda: datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
    )


def test_collects_and_normalizes_google_news_items() -> None:
    articles = asyncio.run(collector_for(RSS_BODY).collect("Example Co funding"))

    assert len(articles) == 1
    article = articles[0]
    assert article.title == "Example & Co raises funding"
    assert article.canonical_url == "https://news.example.com/story?id=42"
    assert article.description == "Funding & expansion announced."
    assert article.published_at == datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    assert article.origin_metadata["query"] == "Example Co funding"
    assert article.retrieved_at == datetime(2026, 8, 12, 7, 0, tzinfo=UTC)


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
        collector = GoogleNewsRSSCollector(client=client)
        try:
            return await collector.collect_many(["success", "timeout", "http", "parse"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert [item.status for item in result.queries] == ["success", "timeout", "http_error", "parse_error"]
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
        collector = GoogleNewsRSSCollector(client=client)
        try:
            return await collector.collect_many(["retry succeeds"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert attempts == 2
    assert len(result.successes) == 1
    assert len(result.articles) == 1


@pytest.mark.parametrize("failure_type", ["read_timeout", "http_status", "parse_error"])
def test_non_connection_failures_are_not_retried(failure_type: str) -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if failure_type == "read_timeout":
            raise httpx.ReadTimeout("read timed out", request=request)
        if failure_type == "http_status":
            return httpx.Response(429, request=request)
        return httpx.Response(200, content=b"not an rss feed", request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = GoogleNewsRSSCollector(client=client)
        try:
            return await collector.collect_many(["no retry"])
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert attempts == 1
    assert len(result.failures) == 1
