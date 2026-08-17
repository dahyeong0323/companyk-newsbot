"""Article-level deduplication before any routing result is rendered."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Iterable

from companyk_newsbot.models import Article


def normalized_title(title: str) -> str:
    """Stable title fingerprint that ignores presentation-only punctuation and case."""
    value = unicodedata.normalize("NFKC", title).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


@dataclass(frozen=True)
class DuplicateArticleGroup:
    primary: Article
    duplicates: tuple[Article, ...]
    reason: str


@dataclass(frozen=True)
class ArticleDeduplicationResult:
    articles: tuple[Article, ...]
    duplicate_groups: tuple[DuplicateArticleGroup, ...]


class ArticleDeduplicator:
    """Collapse same-URL and exact-title RSS/syndication duplicates deterministically."""

    def deduplicate(self, articles: Iterable[Article]) -> ArticleDeduplicationResult:
        retained: list[Article] = []
        groups: dict[int, list[Article]] = {}
        reasons: dict[int, str] = {}
        url_index: dict[str, int] = {}
        title_index: dict[str, int] = {}

        for article in articles:
            canonical_url = article.canonical_url.strip().casefold()
            title = normalized_title(article.title)
            primary_index: int | None = None
            reason = ""
            if canonical_url and canonical_url in url_index:
                primary_index, reason = url_index[canonical_url], "canonical_url"
            elif title and title in title_index:
                primary_index, reason = title_index[title], "normalized_title"

            if primary_index is None:
                primary_index = len(retained)
                retained.append(article)
                if canonical_url:
                    url_index[canonical_url] = primary_index
                if title:
                    title_index[title] = primary_index
                continue

            groups.setdefault(primary_index, []).append(article)
            reasons.setdefault(primary_index, reason)
            retained[primary_index] = self._merge_provenance(retained[primary_index], article)

        return ArticleDeduplicationResult(
            articles=tuple(retained),
            duplicate_groups=tuple(
                DuplicateArticleGroup(
                    primary=retained[index], duplicates=tuple(duplicates), reason=reasons[index]
                )
                for index, duplicates in groups.items()
            ),
        )

    @staticmethod
    def _merge_provenance(primary: Article, duplicate: Article) -> Article:
        """Union query/company provenance while keeping the first article's public semantics."""
        metadata = dict(primary.origin_metadata)
        queries: list[str] = []
        company_ids: list[str] = []
        for article in (primary, duplicate):
            query = article.origin_metadata.get("query")
            if isinstance(query, str) and query.strip():
                queries.append(query)
            origin_queries = article.origin_metadata.get("origin_queries", [])
            if isinstance(origin_queries, list):
                queries.extend(str(value) for value in origin_queries if str(value).strip())
            candidates = article.origin_metadata.get("candidate_company_ids", [])
            if isinstance(candidates, list):
                company_ids.extend(str(value) for value in candidates if str(value).strip())
        if queries:
            metadata["origin_queries"] = list(dict.fromkeys(queries))
        if company_ids:
            metadata["candidate_company_ids"] = list(dict.fromkeys(company_ids))
        return primary.model_copy(update={"origin_metadata": metadata}) if metadata != primary.origin_metadata else primary
