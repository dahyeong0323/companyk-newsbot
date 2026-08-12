from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from companyk_newsbot.collectors.google_news_rss import CollectionError, GoogleNewsRSSCollector, canonicalize_url


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
    client = httpx.Client(transport=transport)
    return GoogleNewsRSSCollector(
        client=client,
        now=lambda: datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
    )


def test_collects_and_normalizes_google_news_items() -> None:
    articles = collector_for(RSS_BODY).collect("Example Co funding")

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
        collector_for(RSS_BODY).collect("  ")


def test_collect_raises_for_http_failure() -> None:
    with pytest.raises(CollectionError, match="request failed"):
        collector_for(b"", status_code=503).collect("Example Co")


def test_canonicalize_url_removes_tracking_without_changing_meaningful_query() -> None:
    assert canonicalize_url("https://example.com/a?gclid=x&id=5&utm_campaign=test#top") == "https://example.com/a?id=5"
