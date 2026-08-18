"""Google News RSS collector with deterministic URL and text normalization."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
import random
import re
import unicodedata
from typing import Any, Awaitable, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from companyk_newsbot.models import Article


GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
TRACKING_PARAMETERS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})


class CollectionError(RuntimeError):
    """Raised when an RSS feed cannot be retrieved or parsed safely."""


class QueryFailure(CollectionError):
    """Typed terminal result after bounded transient retries."""

    def __init__(
        self,
        message: str,
        *,
        status: "QueryStatus",
        attempts: int,
        retry_attempts: int,
        retry_after_used: int = 0,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.attempts = attempts
        self.retry_attempts = retry_attempts
        self.retry_after_used = retry_after_used
        self.http_status = http_status


class QueryParseError(CollectionError):
    """Raised when one RSS response is not a usable feed."""


QueryStatus = Literal[
    "success",
    "rate_limited",
    "service_unavailable",
    "timeout",
    "connection_error",
    "http_error",
    "parse_error",
    "collection_deadline",
    "skipped_systemic_failure",
]


@dataclass(frozen=True)
class QueryCollectionResult:
    """Outcome of exactly one RSS query."""

    query: str
    status: QueryStatus
    articles: tuple[Article, ...] = ()
    error: str | None = None
    attempts: int = 0
    retry_attempts: int = 0
    retry_after_used: int = 0
    http_status: int | None = None


@dataclass(frozen=True)
class RSSCollectionResult:
    """All per-query outcomes from one bounded collection call."""

    queries: tuple[QueryCollectionResult, ...]
    systemic_breaker_triggered: bool = False

    @property
    def articles(self) -> tuple[Article, ...]:
        return tuple(article for result in self.queries for article in result.articles)

    @property
    def successes(self) -> tuple[QueryCollectionResult, ...]:
        return tuple(result for result in self.queries if result.status == "success")

    @property
    def failures(self) -> tuple[QueryCollectionResult, ...]:
        return tuple(result for result in self.queries if result.status != "success")

    def metrics(self) -> dict[str, int | float | bool]:
        total = len(self.queries)
        successes = len(self.successes)
        statuses = {status: sum(item.status == status for item in self.queries) for status in (
            "rate_limited", "service_unavailable", "timeout", "connection_error", "parse_error",
            "skipped_systemic_failure",
        )}
        other_5xx = sum(
            item.status == "http_error" and item.http_status is not None and 500 <= item.http_status <= 599
            for item in self.queries
        )
        return {
            "rss_query_total": total,
            "rss_query_success": successes,
            "rss_query_failure": total - successes,
            "rss_success_ratio": successes / total if total else 0.0,
            "rss_429": statuses["rate_limited"],
            "rss_503": statuses["service_unavailable"],
            "rss_other_5xx": other_5xx,
            "rss_timeout": statuses["timeout"] + sum(item.status == "collection_deadline" for item in self.queries),
            "rss_connection_error": statuses["connection_error"],
            "rss_parse_error": statuses["parse_error"],
            "rss_skipped_systemic_failure": statuses["skipped_systemic_failure"],
            "rss_retry_attempts": sum(item.retry_attempts for item in self.queries),
            "rss_retry_after_used": sum(item.retry_after_used for item in self.queries),
            "rss_systemic_breaker_triggered": self.systemic_breaker_triggered,
        }


@dataclass(frozen=True)
class RSSCollectorSettings:
    """Network and concurrency limits for one RSS collection run."""

    concurrency: int = 6
    connect_timeout_seconds: float = 3.0
    read_timeout_seconds: float = 8.0
    write_timeout_seconds: float = 5.0
    pool_timeout_seconds: float = 3.0
    max_retries: int = 2
    backoff_initial_seconds: float = 0.5
    backoff_max_seconds: float = 4.0
    jitter_ratio: float = 0.2
    retry_after_max_seconds: float = 5.0
    per_query_deadline_seconds: float = 30.0
    collection_deadline_seconds: float = 120.0
    breaker_window: int = 6
    breaker_transient_ratio: float = 0.8
    breaker_probe_queries: int = 2
    breaker_cooldown_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("RSS concurrency must be positive")
        if self.max_retries < 0:
            raise ValueError("RSS retries must not be negative")
        if self.per_query_deadline_seconds <= 0 or self.collection_deadline_seconds <= 0:
            raise ValueError("RSS deadlines must be positive")
        if not 0 <= self.jitter_ratio <= 1 or not 0 < self.breaker_transient_ratio <= 1:
            raise ValueError("RSS jitter and breaker ratio are invalid")
        if self.breaker_window < 3 or self.breaker_probe_queries < 1:
            raise ValueError("RSS breaker settings are invalid")

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
        language: str = "ko-KR",
        country: str = "KR",
        settings: RSSCollectorSettings | None = None,
        freshness_hint: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
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
        self.freshness_hint = freshness_hint.strip() if freshness_hint else None
        self._sleep = sleep
        self._random_value = random_value
        self.request_headers = {
            "User-Agent": "CompanyK-Newsbot/1.0 (+Google-News-RSS)",
            "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8",
            "Accept-Language": f"{language},en;q=0.8",
        }
        if self.freshness_hint and not re.fullmatch(r"when:\d+[hd]", self.freshness_hint):
            raise ValueError("Google News freshness hint must look like when:2d or when:24h")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "GoogleNewsRSSCollector":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @staticmethod
    def _retry_after_seconds(response: httpx.Response, maximum: float) -> float | None:
        raw = response.headers.get("Retry-After", "").strip()
        if not raw:
            return None
        try:
            seconds = float(raw)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                seconds = (retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        return min(maximum, max(0.0, seconds))

    def _backoff_seconds(self, retry_index: int) -> float:
        base = min(self.settings.backoff_max_seconds, self.settings.backoff_initial_seconds * (2 ** retry_index))
        jitter = 1 + self.settings.jitter_ratio * ((2 * self._random_value()) - 1)
        return max(0.0, base * jitter)

    async def _collect_with_metadata(self, query: str) -> tuple[list[Article], int, int, int]:
        query = query.strip()
        if not query:
            raise ValueError("Google News RSS query must not be blank")
        runtime_query = f"{query} {self.freshness_hint}" if self.freshness_hint else query
        edition_language = self.language.split("-", 1)[0].casefold()
        params = {
            "q": runtime_query,
            "hl": self.language,
            "gl": self.country,
            "ceid": f"{self.country}:{edition_language}",
        }
        retry_after_used = 0
        for attempt_index in range(self.settings.max_retries + 1):
            attempts = attempt_index + 1
            failure: httpx.HTTPError | None = None
            code: int | None = None
            try:
                response = await self._client.get(GOOGLE_NEWS_RSS_URL, params=params, headers=self.request_headers)
                if response.status_code >= 400:
                    response.raise_for_status()
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as caught:
                failure = caught
                status: QueryStatus = "connection_error"
                transient = True
            except httpx.TimeoutException as caught:
                failure = caught
                status = "timeout"
                transient = True
            except httpx.HTTPStatusError as caught:
                failure = caught
                code = caught.response.status_code
                status = "rate_limited" if code == 429 else "service_unavailable" if code == 503 else "http_error"
                transient = code in {429, 502, 503, 504}
            except httpx.HTTPError as caught:
                failure = caught
                status = "http_error"
                transient = False

            if failure is not None:
                if transient and attempt_index < self.settings.max_retries:
                    retry_after = (
                        self._retry_after_seconds(failure.response, self.settings.retry_after_max_seconds)
                        if isinstance(failure, httpx.HTTPStatusError) and code in {429, 503} else None
                    )
                    if retry_after is not None:
                        retry_after_used += 1
                        delay = retry_after
                    else:
                        delay = self._backoff_seconds(attempt_index)
                    await self._sleep(delay)
                    continue
                raise QueryFailure(
                    f"Google News RSS request failed for {query!r} after {attempts} attempts: {failure}",
                    status=status,
                    attempts=attempts,
                    retry_attempts=attempt_index,
                    retry_after_used=retry_after_used,
                    http_status=code,
                ) from failure

        parsed = feedparser.parse(response.content)
        if parsed.bozo and not parsed.entries:
            raise QueryFailure(
                f"Google News RSS response could not be parsed for {query!r}",
                status="parse_error", attempts=attempts, retry_attempts=attempt_index,
                retry_after_used=retry_after_used,
            )

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
        return articles, attempts, attempt_index, retry_after_used

    async def collect(self, query: str) -> list[Article]:
        articles, _, _, _ = await self._collect_with_metadata(query)
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

        outcomes: list[QueryCollectionResult | None] = [None] * len(query_list)
        loop = asyncio.get_running_loop()
        started = loop.time()
        recent_transient: deque[bool] = deque(maxlen=self.settings.breaker_window)
        systemic_breaker_triggered = False
        index = 0

        async def run_one(item_index: int) -> QueryCollectionResult:
            query = query_list[item_index]
            try:
                articles, attempts, retries, retry_after_used = await asyncio.wait_for(
                    self._collect_with_metadata(query), timeout=self.settings.per_query_deadline_seconds,
                )
                return QueryCollectionResult(query, "success", tuple(articles), attempts=attempts,
                                             retry_attempts=retries, retry_after_used=retry_after_used)
            except QueryFailure as exc:
                return QueryCollectionResult(
                    query, exc.status, error=str(exc), attempts=exc.attempts,
                    retry_attempts=exc.retry_attempts, retry_after_used=exc.retry_after_used,
                    http_status=exc.http_status,
                )
            except TimeoutError:
                return QueryCollectionResult(
                    query, "timeout", error=f"Google News RSS hard deadline exceeded after {self.settings.per_query_deadline_seconds:g}s",
                    attempts=1,
                )

        transient_statuses = {"rate_limited", "service_unavailable", "timeout", "connection_error"}
        while index < len(query_list):
            remaining = self.settings.collection_deadline_seconds - (loop.time() - started)
            if remaining <= 0:
                break
            batch_indexes = list(range(index, min(index + self.settings.concurrency, len(query_list))))
            tasks = {asyncio.create_task(run_one(i)): i for i in batch_indexes}
            done, pending = await asyncio.wait(tasks, timeout=remaining)
            for task in done:
                item_index = tasks[task]
                result = task.result()
                outcomes[item_index] = result
                recent_transient.append(result.status in transient_statuses)
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in pending:
                    item_index = tasks[task]
                    outcomes[item_index] = QueryCollectionResult(
                        query_list[item_index], "collection_deadline",
                        error=f"RSS collection deadline exceeded after {self.settings.collection_deadline_seconds:g}s",
                    )
                index = max(batch_indexes) + 1
                break
            index = max(batch_indexes) + 1

            if len(recent_transient) == self.settings.breaker_window and (
                sum(recent_transient) / len(recent_transient) >= self.settings.breaker_transient_ratio
            ):
                systemic_breaker_triggered = True
                remaining = self.settings.collection_deadline_seconds - (loop.time() - started)
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(
                        self._sleep(self.settings.breaker_cooldown_seconds), timeout=remaining,
                    )
                except TimeoutError:
                    break
                probe_end = min(index + self.settings.breaker_probe_queries, len(query_list))
                recovered = False
                deadline_hit = False
                while index < probe_end:
                    remaining = self.settings.collection_deadline_seconds - (loop.time() - started)
                    if remaining <= 0:
                        deadline_hit = True
                        break
                    try:
                        result = await asyncio.wait_for(run_one(index), timeout=remaining)
                    except TimeoutError:
                        outcomes[index] = QueryCollectionResult(
                            query_list[index], "collection_deadline",
                            error=f"RSS collection deadline exceeded after {self.settings.collection_deadline_seconds:g}s",
                        )
                        index += 1
                        deadline_hit = True
                        break
                    outcomes[index] = result
                    index += 1
                    if result.status == "success":
                        recovered = True
                        recent_transient.clear()
                        break
                if deadline_hit:
                    break
                if not recovered:
                    for skipped_index in range(index, len(query_list)):
                        outcomes[skipped_index] = QueryCollectionResult(
                            query_list[skipped_index], "skipped_systemic_failure",
                            error="RSS systemic-failure circuit breaker opened before request",
                        )
                    index = len(query_list)

        for remaining_index, outcome in enumerate(outcomes):
            if outcome is None:
                outcomes[remaining_index] = QueryCollectionResult(
                    query_list[remaining_index], "collection_deadline",
                    error=f"RSS collection deadline exceeded after {self.settings.collection_deadline_seconds:g}s before request",
                )
        return RSSCollectionResult(
            queries=tuple(outcome for outcome in outcomes if outcome is not None),
            systemic_breaker_triggered=systemic_breaker_triggered,
        )

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
            origin_metadata={
                "query": query,
                "origin_queries": [query],
                "feed_url": feed_url,
                "feed_index": index,
                "google_news_url": url,
            },
        )
