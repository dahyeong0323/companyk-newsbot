"""Free, deterministic publisher-page enrichment for query-scoped Route A candidates."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from html import unescape
import ipaddress
import json
import os
import re
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
import httpx

from companyk_newsbot.collectors.google_news_rss import canonicalize_url
from companyk_newsbot.models import Article
from companyk_newsbot.portfolio_registry import PortfolioRegistry
from companyk_newsbot.rules import RouteADetector


EnrichmentStatus = Literal[
    "success", "blocked", "timeout", "http_error", "parse_error", "insufficient_content"
]
MAX_HTML_BYTES = 2_000_000
MAX_ENRICHED_CHARS = 20_000
MIN_ENRICHED_CHARS = 20
GOOGLE_HOSTS = frozenset({"news.google.com", "google.com", "www.google.com"})
_CHROME_MARKER = re.compile(r"(?:related|recommended|recommendation|footer|sidebar|navigation|breadcrumb|promo|advert|outbrain|taboola)", re.I)


class BlockedEnrichment(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractedArticle:
    text: str
    source: Literal["jsonld", "article_tag", "main_tag", "opengraph"]


@dataclass(frozen=True)
class FetchResult:
    status: EnrichmentStatus
    resolved_url: str | None = None
    text: str | None = None
    source: str | None = None


@dataclass
class EnrichmentMetrics:
    enrichment_candidates: int = 0
    enrichment_skipped_identity_already_visible: int = 0
    enrichment_attempted: int = 0
    enrichment_success: int = 0
    enrichment_timeout: int = 0
    enrichment_http_error: int = 0
    enrichment_blocked: int = 0
    enrichment_parse_error: int = 0
    enrichment_insufficient_content: int = 0
    resolved_publisher_urls: int = 0
    same_run_enrichment_cache_hits: int = 0
    route_a_matches_from_rss: int = 0
    route_a_matches_from_enriched_content: int = 0

    def payload(self) -> dict[str, int]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class EnrichmentResult:
    articles: tuple[Article, ...]
    metrics: EnrichmentMetrics


def _normalized_text(value: str) -> str:
    return " ".join(unescape(value).split())


def _jsonld_articles(value):
    if isinstance(value, list):
        for item in value:
            yield from _jsonld_articles(item)
    elif isinstance(value, dict):
        graph = value.get("@graph")
        if graph is not None:
            yield from _jsonld_articles(graph)
        types = value.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if any(str(item).casefold() in {"article", "newsarticle", "reportagenewsarticle"} for item in types):
            yield value


def extract_article_text(html: str) -> ExtractedArticle | None:
    """Extract article-specific evidence; arbitrary page chrome is never accepted as content."""
    soup = BeautifulSoup(html, "html.parser")
    metadata: list[str] = []
    if soup.title and soup.title.string:
        metadata.append(str(soup.title.string))
    for attrs in (
        {"property": "og:title"}, {"property": "og:description"},
        {"name": "description"}, {"name": "twitter:title"}, {"name": "twitter:description"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and isinstance(tag.get("content"), str):
            metadata.append(tag["content"])

    jsonld_parts: list[str] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        for article in _jsonld_articles(value):
            for key in ("headline", "description", "articleBody"):
                content = article.get(key)
                if isinstance(content, str) and content.strip():
                    jsonld_parts.append(content)
    if jsonld_parts:
        text = _normalized_text(" ".join((*metadata, *jsonld_parts)))[:MAX_ENRICHED_CHARS]
        if len(text) >= MIN_ENRICHED_CHARS:
            return ExtractedArticle(text, "jsonld")

    for tag_name, source in (("article", "article_tag"), ("main", "main_tag")):
        container = soup.find(tag_name)
        if container:
            for unwanted in container.find_all(("script", "style", "noscript", "template", "svg", "nav", "footer", "aside", "form")):
                unwanted.decompose()
            for candidate in list(container.find_all(True)):
                marker = " ".join((
                    str(candidate.get("id", "")),
                    " ".join(str(value) for value in candidate.get("class", [])),
                    str(candidate.get("role", "")),
                    str(candidate.get("aria-label", "")),
                ))
                if _CHROME_MARKER.search(marker):
                    candidate.decompose()
            text = _normalized_text(" ".join((*metadata, container.get_text(" ", strip=True))))[:MAX_ENRICHED_CHARS]
            if len(text) >= MIN_ENRICHED_CHARS:
                return ExtractedArticle(text, source)

    meta_text = _normalized_text(" ".join(metadata))[:MAX_ENRICHED_CHARS]
    if len(meta_text) >= MIN_ENRICHED_CHARS and len(metadata) >= 2:
        return ExtractedArticle(meta_text, "opengraph")
    return None


def _safe_http_url(value: str) -> bool:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return False
    host = parts.hostname.casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast)


def _is_google_news_url(value: str) -> bool:
    host = (urlsplit(value).hostname or "").casefold()
    return host in GOOGLE_HOSTS or host.endswith(".google.com")


def google_publisher_resolution_url(value: str) -> str:
    """Use Google's responsive article route; the RSS wrapper route commonly returns a slow 503."""
    parts = urlsplit(value)
    if (parts.hostname or "").casefold() != "news.google.com" or not parts.path.startswith("/rss/articles/"):
        return value
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({"hl": "en-US", "gl": "US", "ceid": "US:en"})
    return urlunsplit((parts.scheme, parts.netloc, parts.path.removeprefix("/rss"), urlencode(query), ""))


def _publisher_url_from_google_html(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for tag, attribute in ((soup.find("link", rel="canonical"), "href"),
                           (soup.find("meta", property="og:url"), "content")):
        if tag and isinstance(tag.get(attribute), str):
            candidates.append(urljoin(base_url, tag[attribute]))
    for tag in soup.find_all("a", href=True):
        candidates.append(urljoin(base_url, tag["href"]))
    for candidate in candidates:
        if _safe_http_url(candidate) and not _is_google_news_url(candidate):
            return candidate
    return None


def _google_decoder_attributes(html: str) -> tuple[str, str, str] | None:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find(attrs={"data-n-a-id": True, "data-n-a-ts": True, "data-n-a-sg": True})
    if not tag:
        return None
    values = (tag.get("data-n-a-id"), tag.get("data-n-a-ts"), tag.get("data-n-a-sg"))
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    return values  # type: ignore[return-value]


def _decoded_publisher_url(value) -> str | None:
    if isinstance(value, list):
        if len(value) >= 2 and value[0] == "garturlres" and isinstance(value[1], str):
            return value[1] if _safe_http_url(value[1]) and not _is_google_news_url(value[1]) else None
        for item in value:
            found = _decoded_publisher_url(item)
            if found:
                return found
    elif isinstance(value, dict):
        for item in value.values():
            found = _decoded_publisher_url(item)
            if found:
                return found
    elif isinstance(value, str) and "garturlres" in value:
        try:
            return _decoded_publisher_url(json.loads(value))
        except json.JSONDecodeError:
            return None
    return None


class PublisherArticleEnricher:
    """Bounded async enrichment with exact same-run fetch caching and per-host caps."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        concurrency: int = 8,
        per_host_concurrency: int = 2,
        timeout_seconds: float = 8.0,
        max_redirects: int = 4,
    ) -> None:
        if concurrency < 1 or per_host_concurrency < 1 or timeout_seconds <= 0 or max_redirects < 0:
            raise ValueError("enrichment limits must be positive")
        self.concurrency = concurrency
        self.per_host_concurrency = per_host_concurrency
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max_redirects
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
            headers={"User-Agent": "Mozilla/5.0 (compatible; CompanyKNewsbot/1.0; public-news-enrichment)"},
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._host_locks: dict[str, asyncio.Semaphore] = {}
        self._cache: dict[str, asyncio.Task[FetchResult]] = {}
        self._host_failures: dict[str, int] = {}
        self._blocked_hosts: set[str] = set()
        self._host_failure_threshold = int(os.getenv("ENRICHMENT_HOST_FAILURE_THRESHOLD", "3"))
        self.metrics = EnrichmentMetrics()

    @classmethod
    def from_environment(cls) -> "PublisherArticleEnricher":
        return cls(
            concurrency=int(os.getenv("ENRICHMENT_CONCURRENCY", "8")),
            per_host_concurrency=int(os.getenv("ENRICHMENT_PER_HOST_CONCURRENCY", "2")),
            timeout_seconds=float(os.getenv("ENRICHMENT_TIMEOUT_SECONDS", "8")),
            max_redirects=int(os.getenv("ENRICHMENT_MAX_REDIRECTS", "4")),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def enrich_all(self, articles: list[Article], registry: PortfolioRegistry) -> EnrichmentResult:
        detector = RouteADetector(registry)
        scoped = [detector.with_candidate_provenance(article) for article in articles]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def enrich_one(article: Article) -> Article:
            candidate_ids = detector.candidate_company_ids(article)
            if not candidate_ids:
                return article
            rss_matches = detector.detect_scoped(article, include_text=False)
            if rss_matches:
                self.metrics.enrichment_skipped_identity_already_visible += 1
                self.metrics.route_a_matches_from_rss += len(rss_matches)
                return self._with_audit(article, FetchResult("success"), attempted=False, status="not_needed")
            self.metrics.enrichment_candidates += 1
            key = article.canonical_url.strip().casefold() or article.url.strip().casefold()
            if key in self._cache:
                self.metrics.same_run_enrichment_cache_hits += 1
                fetched = await self._cache[key]
            else:
                async def bounded_fetch() -> FetchResult:
                    async with semaphore:
                        return await self._fetch(article.url)
                self._cache[key] = asyncio.create_task(bounded_fetch())
                fetched = await self._cache[key]
            enriched = self._with_audit(article, fetched, attempted=True)
            if fetched.status == "success":
                self.metrics.enrichment_success += 1
                if fetched.resolved_url and not _is_google_news_url(fetched.resolved_url):
                    self.metrics.resolved_publisher_urls += 1
                self.metrics.route_a_matches_from_enriched_content += len(detector.detect_scoped(enriched))
            else:
                setattr(self.metrics, f"enrichment_{fetched.status}", getattr(self.metrics, f"enrichment_{fetched.status}") + 1)
            return enriched

        try:
            enriched = await asyncio.gather(*(enrich_one(article) for article in scoped))
            return EnrichmentResult(tuple(enriched), self.metrics)
        finally:
            await self.close()

    async def _fetch(self, original_url: str) -> FetchResult:
        if not _safe_http_url(original_url):
            return FetchResult("blocked")
        request_url = google_publisher_resolution_url(original_url)
        initial_host = (urlsplit(request_url).hostname or "").casefold()
        if initial_host in self._blocked_hosts:
            return FetchResult("blocked")
        self.metrics.enrichment_attempted += 1
        try:
            response, final_url = await self._get_with_redirects(request_url)
            html = self._bounded_html(response)
            if _is_google_news_url(final_url):
                publisher_url = _publisher_url_from_google_html(html, final_url)
                if not publisher_url:
                    attributes = _google_decoder_attributes(html)
                    if attributes:
                        publisher_url = await self._decode_google_article(*attributes)
                if not publisher_url:
                    return FetchResult("blocked", resolved_url=final_url)
                response, final_url = await self._get_with_redirects(publisher_url)
                html = self._bounded_html(response)
            content_type = response.headers.get("content-type", "").casefold()
            if content_type and "html" not in content_type and "xhtml" not in content_type:
                return FetchResult("parse_error", resolved_url=final_url)
            extracted = extract_article_text(html)
            if extracted is None:
                return FetchResult("insufficient_content", resolved_url=final_url)
            return FetchResult("success", canonicalize_url(final_url), extracted.text, extracted.source)
        except httpx.TimeoutException as exc:
            self._record_host_failure(self._failure_host(exc, initial_host))
            return FetchResult("timeout")
        except BlockedEnrichment:
            return FetchResult("blocked")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            self._record_host_failure(self._failure_host(exc, initial_host))
            return FetchResult("blocked" if status in {401, 403, 407, 429, 451} else "http_error")
        except (httpx.HTTPError, UnicodeError) as exc:
            self._record_host_failure(self._failure_host(exc, initial_host))
            return FetchResult("http_error")
        except Exception:
            return FetchResult("parse_error")

    async def _decode_google_article(self, article_id: str, timestamp: str, signature: str) -> str | None:
        payload = [
            "garturlreq",
            [["en-US", "US", ["FINANCE_TOP_INDICES", "WEB_TEST_1_0_0"], None, None, 1, 1,
              "US:en", None, 180, None, None, None, None, None, 0, None, None,
              [1608992183, 723341000]], "en-US", "US", 1, [2, 3, 4, 8], 1, 0,
             "655000234", 0, 0, None, 0],
            article_id,
            int(timestamp),
            signature,
        ]
        request_body = json.dumps([[["Fbv4je", json.dumps(payload, separators=(",", ":")), None, "generic"]]])
        url = "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je"
        lock = self._host_locks.setdefault("news.google.com", asyncio.Semaphore(self.concurrency))
        async with lock:
            response = await self._client.post(
                url,
                data={"f.req": request_body},
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            )
        response.raise_for_status()
        for line in response.text.splitlines():
            line = line.strip()
            if not line.startswith("["):
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            found = _decoded_publisher_url(value)
            if found:
                return found
        return None

    async def _get_with_redirects(self, url: str) -> tuple[httpx.Response, str]:
        current = url
        for redirect_count in range(self.max_redirects + 1):
            if not _safe_http_url(current):
                raise BlockedEnrichment("unsafe enrichment URL")
            host = (urlsplit(current).hostname or "").casefold()
            host_limit = self.concurrency if _is_google_news_url(current) else self.per_host_concurrency
            lock = self._host_locks.setdefault(host, asyncio.Semaphore(host_limit))
            async with lock:
                response = await self._client.get(current, follow_redirects=False)
            if response.is_redirect:
                if redirect_count == self.max_redirects:
                    raise httpx.TooManyRedirects("enrichment redirect limit exceeded", request=response.request)
                location = response.headers.get("location")
                if not location:
                    raise httpx.HTTPStatusError("redirect missing Location", request=response.request, response=response)
                current = urljoin(str(response.url), location)
                continue
            response.raise_for_status()
            if len(response.content) > MAX_HTML_BYTES:
                return response, str(response.url)
            return response, str(response.url)
        raise httpx.TooManyRedirects("enrichment redirect limit exceeded")

    @staticmethod
    def _bounded_html(response: httpx.Response) -> str:
        content = response.content[:MAX_HTML_BYTES]
        encoding = response.encoding or "utf-8"
        return content.decode(encoding, errors="replace")

    def _record_host_failure(self, host: str) -> None:
        if not host:
            return
        self._host_failures[host] = self._host_failures.get(host, 0) + 1
        if self._host_failures[host] >= self._host_failure_threshold:
            self._blocked_hosts.add(host)

    @staticmethod
    def _failure_host(exc: Exception, fallback: str) -> str:
        """Attribute a fetch failure to the request that actually failed.

        Google News is commonly only the redirect wrapper.  Counting a
        publisher failure against that wrapper can otherwise trip its circuit
        breaker and suppress unrelated publisher enrichment.
        """
        request = getattr(exc, "request", None)
        url = getattr(request, "url", None)
        return (getattr(url, "host", None) or fallback).casefold()

    @staticmethod
    def _with_audit(article: Article, fetched: FetchResult, *, attempted: bool, status: str | None = None) -> Article:
        metadata = dict(article.origin_metadata)
        metadata.update({
            "enrichment_attempted": attempted,
            "enrichment_status": status or fetched.status,
            "resolved_url": fetched.resolved_url,
            "enrichment_source": fetched.source,
            "enriched_char_count": len(fetched.text or ""),
        })
        updates: dict[str, object] = {"origin_metadata": metadata}
        if fetched.status == "success" and fetched.text:
            updates["text"] = fetched.text
            if fetched.resolved_url and not _is_google_news_url(fetched.resolved_url):
                updates["canonical_url"] = fetched.resolved_url
        return article.model_copy(update=updates)
