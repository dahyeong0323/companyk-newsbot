"""Exposure-only Route B candidate generation; no causal judgment occurs here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import unicodedata
from typing import Iterable

from companyk_newsbot.config import Exposure, KeywordMapConfig
from companyk_newsbot.models import Article


def _query_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


@dataclass(frozen=True)
class ExposureLink:
    """One portfolio-company impact path attached to an exposure search term."""

    company: str
    exposure_id: str
    subject: str
    query_term: str
    valid_from: date
    allowed_event_families: tuple[str, ...]
    exposure: Exposure


@dataclass(frozen=True)
class ExposureQuery:
    """A de-duplicated collector query with all company/exposure attachments."""

    query: str
    links: tuple[ExposureLink, ...]


@dataclass(frozen=True)
class RouteBCandidate:
    article: Article
    company: str
    exposure_id: str
    exposure_subject: str
    allowed_event_families: tuple[str, ...]


@dataclass(frozen=True)
class RouteBRejection:
    article: Article
    reason: str
    detail: str


@dataclass(frozen=True)
class CandidateGenerationResult:
    candidates: tuple[RouteBCandidate, ...]
    rejections: tuple[RouteBRejection, ...]


class ExposureRegistry:
    """Normalized, query-first view of registered company-specific exposures."""

    def __init__(self, config: KeywordMapConfig) -> None:
        by_query: dict[str, list[ExposureLink]] = {}
        display_queries: dict[str, str] = {}
        for company, company_rule in config.company_rules.items():
            for exposure in company_rule.external_exposures or []:
                for query_term in exposure.subject.query_terms:
                    key = _query_key(query_term)
                    link = ExposureLink(
                        company=company,
                        exposure_id=exposure.exposure_id,
                        subject=exposure.subject.canonical,
                        query_term=query_term,
                        valid_from=exposure.valid_from,
                        allowed_event_families=tuple(exposure.allowed_event_families),
                        exposure=exposure,
                    )
                    by_query.setdefault(key, []).append(link)
                    display_queries.setdefault(key, query_term)
        self._queries = {
            key: ExposureQuery(query=display_queries[key], links=tuple(links))
            for key, links in by_query.items()
        }

    @property
    def queries(self) -> tuple[ExposureQuery, ...]:
        return tuple(sorted(self._queries.values(), key=lambda query: _query_key(query.query)))

    def lookup(self, query: str) -> ExposureQuery | None:
        return self._queries.get(_query_key(query))


class RouteBCandidateGenerator:
    """Create candidates only from a registered collector-query → exposure link."""

    def __init__(self, registry: ExposureRegistry) -> None:
        self.registry = registry

    def generate(self, articles: Iterable[Article]) -> CandidateGenerationResult:
        candidates: list[RouteBCandidate] = []
        rejections: list[RouteBRejection] = []
        for article in articles:
            query = article.origin_metadata.get("query")
            if not isinstance(query, str) or not query.strip():
                rejections.append(RouteBRejection(article, "missing_registered_exposure_query", "article has no collector query"))
                continue
            exposure_query = self.registry.lookup(query)
            if exposure_query is None:
                rejections.append(RouteBRejection(article, "unregistered_query", query))
                continue
            if article.published_at is None:
                rejections.append(RouteBRejection(article, "missing_published_at", "knowledge-time guard cannot be evaluated"))
                continue
            event_date = article.published_at.date()
            for link in exposure_query.links:
                if event_date < link.valid_from:
                    rejections.append(
                        RouteBRejection(
                            article,
                            "before_exposure_valid_from",
                            f"{link.exposure_id}: event={event_date.isoformat()} valid_from={link.valid_from.isoformat()}",
                        )
                    )
                    continue
                candidates.append(
                    RouteBCandidate(
                        article=article,
                        company=link.company,
                        exposure_id=link.exposure_id,
                        exposure_subject=link.subject,
                        allowed_event_families=link.allowed_event_families,
                    )
                )
        return CandidateGenerationResult(candidates=tuple(candidates), rejections=tuple(rejections))
