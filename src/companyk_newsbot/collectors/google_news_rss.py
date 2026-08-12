"""Google News RSS collector with deterministic URL and text normalization."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
import re
import unicodedata
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from companyk_newsbot.models import Article


GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
TRACKING_PARAMETERS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})


class CollectionError(RuntimeError):
    """Raised when an RSS feed cannot be retrieved or parsed safely."""


class QueryTimeoutError(CollectionError):
    """Raised when one RSS query exceeds an HTTPX network timeout."""


class QueryHTTPError(CollectionError):
    """Raised when one RSS query fails at the HTTP layer."""


class QueryParseError(CollectionError):
    """Raised when one RSS response is not a usable feed."""


QueryStatus = Literal["success", "timeout", "http_error", "parse_error"]


@dataclass(frozen=True)
class QueryCollectionResult:
    """Outcome of exactly one RSS query."""

    query: str
    status: QueryStatus
    articles: tuple[Article, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class RSSCollectionResult:
    """All per-query outcomes from one bounded collection call."""

    queries: tuple[QueryCollectionResult, ...]

    @property
    def articles(self) -> tuple[Article, ...]:
        return tuple(article for result in self.queries for article in result.articles)

    @property
    def successes(self) -> tuple[QueryCollectionResult, ...]:
        return tuple(result for result in self.queries if result.status == "success")

    @property
    def failures(self) -> tuple[QueryCollectionResult, ...]:
        return tuple(result for result in self.queries if result.status != "success")


@dataclass(frozen=True)
class RSSCollectorSettings:
    """Network and concurrency limits for one RSS collection run."""

    concurrency: int = 6
    connect_timeout_seconds: float = 3.0
    read_timeout_seconds: float = 8.0
    write_timeout_seconds: float = 5.0
    pool_timeout_seconds: float = 3.0
    max_connection_retries: int = 1
    per_query_deadline_seconds: float = 12.0
    collection_deadline_seconds: float = 75.0

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("RSS concurrency must be positive")
        if self.max_connection_retries < 0:
            raise ValueError("RSS connection retries must not be negative")
        if self.per_query_deadline_seconds <= 0 or self.collection_deadline_seconds <= 0:
            raise ValueError("RSS deadlines must be positive")

    @property
    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout_seconds,
            read=self.read_timeout_seconds,
            write=self.write_timeout_seconds,
            pool=self.pool_timeout_seconds,
        )

    @property
    def limits(self) -> httpx.Limits:
        return httpx.Limits(
            max_connections=self.concurrency,
            max_keepalive_connections=self.concurrency,
        )


def canonicalize_url(url: str) -> str:
    """Strip fragments and common tracking parameters without resolving redirects."""
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))


def normalized_query(query: str) -> str:
    """Stable query identity used to prevent duplicate RSS requests."""
    return " ".join(unicodedata.normalize("NFKC", query).casefold().split())


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
    """Collect explicit Google News RSS queries with bounded concurrency."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] | None = None,
        language: str = "en-US",
        country: str = "US",
        settings: RSSCollectorSettings | None = None,
    ) -> None:
        self.settings = settings or RSSCollectorSettings()
        self._client = client or httpx.AsyncClient(
            timeout=self.settings.timeout,
            limits=self.settings.limits,
            follow_redirects=True,
        )
        self._owns_client = client is None
        self._now = now or (lambda: datetime.now(UTC))
        self.language = language
        self.country = country

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "GoogleNewsRSSCollector":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def collect(self, query: str) -> list[Article]:
        query = query.strip()
        if not query:
            raise ValueError("Google News RSS query must not be blank")
        params = {"q": query, "hl": self.language, "gl": self.country, "ceid": f"{self.country}:en"}
        connection_failures = 0
        while True:
            try:
                response = await self._client.get(GOOGLE_NEWS_RSS_URL, params=params)
                response.raise_for_status()
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if connection_failures < self.settings.max_connection_retries:
                    connection_failures += 1
                    continue
                attempts = connection_failures + 1
                if isinstance(exc, httpx.ConnectTimeout):
                    raise QueryTimeoutError(
                        f"Google News RSS connection timed out for {query!r} after {attempts} attempts: {exc}"
                    ) from exc
                raise QueryHTTPError(
                    f"Google News RSS connection failed for {query!r} after {attempts} attempts: {exc}"
                ) from exc
            except httpx.TimeoutException as exc:
                raise QueryTimeoutError(f"Google News RSS request timed out for {query!r}: {exc}") from exc
            except httpx.HTTPError as exc:
                raise QueryHTTPError(f"Google News RSS request failed for {query!r}: {exc}") from exc

        parsed = feedparser.parse(response.content)
        if parsed.bozo and not parsed.entries:
            raise QueryParseError(f"Google News RSS response could not be parsed for {query!r}")

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

    async def collect_many(self, queries: Iterable[str]) -> RSSCollectionResult:
        """Collect unique queries within per-query and whole-run wall-clock deadlines."""
        query_list: list[str] = []
        seen: set[str] = set()
        for query in queries:
            key = normalized_query(query)
            if not key:
                raise ValueError("Google News RSS query must not be blank")
            if key not in seen:
                seen.add(key)
                query_list.append(query.strip())
        if not query_list:
            return RSSCollectionResult(queries=())

        queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        for index, query in enumerate(query_list):
            queue.put_nowait((index, query))
        outcomes: list[QueryCollectionResult | None] = [None] * len(query_list)

        async def worker() -> None:
            while True:
                try:
                    index, query = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    articles = await asyncio.wait_for(
                        self.collect(query),
                        timeout=self.settings.per_query_deadline_seconds,
                    )
                    outcomes[index] = QueryCollectionResult(
                        query=query,
                        status="success",
                        articles=tuple(articles),
                    )
                except QueryTimeoutError as exc:
                    outcomes[index] = QueryCollectionResult(query=query, status="timeout", error=str(exc))
                except QueryHTTPError as exc:
                    outcomes[index] = QueryCollectionResult(query=query, status="http_error", error=str(exc))
                except QueryParseError as exc:
                    outcomes[index] = QueryCollectionResult(query=query, status="parse_error", error=str(exc))
                except TimeoutError:
                    outcomes[index] = QueryCollectionResult(
                        query=query,
                        status="timeout",
                        error=f"Google News RSS hard deadline exceeded after {self.settings.per_query_deadline_seconds:g}s",
                    )
                finally:
                    queue.task_done()

        worker_count = min(self.settings.concurrency, len(query_list))
        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        done, pending = await asyncio.wait(workers, timeout=self.settings.collection_deadline_seconds)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for index, outcome in enumerate(outcomes):
                if outcome is None:
                    outcomes[index] = QueryCollectionResult(
                        query=query_list[index],
                        status="timeout",
                        error=f"RSS collection deadline exceeded after {self.settings.collection_deadline_seconds:g}s",
                    )
        await asyncio.gather(*done, return_exceptions=False)
        return RSSCollectionResult(queries=tuple(outcome for outcome in outcomes if outcome is not None))

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
