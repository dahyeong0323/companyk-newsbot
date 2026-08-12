"""Google News RSS collector with deterministic URL and text normalization."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from companyk_newsbot.models import Article


GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
TRACKING_PARAMETERS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})


class CollectionError(RuntimeError):
    """Raised when an RSS feed cannot be retrieved or parsed safely."""


def canonicalize_url(url: str) -> str:
    """Strip fragments and common tracking parameters without resolving redirects."""
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))


def plain_text(value: str | None) -> str | None:
    """Remove RSS summary markup while preserving a concise, readable description."""
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", unescape(value))
    normalized = " ".join(text.split())
    return normalized or None


def parse_published_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class GoogleNewsRSSCollector:
    """Collect Google News RSS results for explicit queries only."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        now: Callable[[], datetime] | None = None,
        language: str = "en-US",
        country: str = "US",
    ) -> None:
        self._client = client or httpx.Client(timeout=20.0, follow_redirects=True)
        self._owns_client = client is None
        self._now = now or (lambda: datetime.now(UTC))
        self.language = language
        self.country = country

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GoogleNewsRSSCollector":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def collect(self, query: str) -> list[Article]:
        query = query.strip()
        if not query:
            raise ValueError("Google News RSS query must not be blank")
        params = {"q": query, "hl": self.language, "gl": self.country, "ceid": f"{self.country}:en"}
        try:
            response = self._client.get(GOOGLE_NEWS_RSS_URL, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CollectionError(f"Google News RSS request failed for {query!r}: {exc}") from exc

        parsed = feedparser.parse(response.content)
        if parsed.bozo and not parsed.entries:
            raise CollectionError(f"Google News RSS response could not be parsed for {query!r}")

        retrieved_at = self._now()
        if retrieved_at.tzinfo is None:
            retrieved_at = retrieved_at.replace(tzinfo=UTC)
        else:
            retrieved_at = retrieved_at.astimezone(UTC)
        feed_language = parsed.feed.get("language") or self.language
        articles: list[Article] = []
        for index, entry in enumerate(parsed.entries):
            article = self._normalize_entry(
                entry,
                query=query,
                retrieved_at=retrieved_at,
                language=feed_language,
                feed_url=str(response.url),
                index=index,
            )
            if article:
                articles.append(article)
        return articles

    def collect_many(self, queries: Iterable[str]) -> list[Article]:
        """Collect each explicit query; cross-query deduplication belongs to Step 4."""
        return [article for query in queries for article in self.collect(query)]

    @staticmethod
    def _normalize_entry(
        entry: dict[str, Any],
        *,
        query: str,
        retrieved_at: datetime,
        language: str,
        feed_url: str,
        index: int,
    ) -> Article | None:
        title = plain_text(entry.get("title"))
        url = entry.get("link")
        if not title or not isinstance(url, str) or not url.strip():
            return None
        description = plain_text(entry.get("summary") or entry.get("description"))
        return Article(
            source="Google News",
            source_type="google_news_rss",
            title=title,
            url=url,
            canonical_url=canonicalize_url(url),
            published_at=parse_published_at(entry.get("published")),
            retrieved_at=retrieved_at,
            description=description,
            language=language,
            origin_metadata={"query": query, "feed_url": feed_url, "feed_index": index},
        )
