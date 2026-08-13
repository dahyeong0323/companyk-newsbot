"""Shared deterministic representative-article selection for both routes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, TypeVar, Callable
from urllib.parse import urlparse

from companyk_newsbot.dedup.anchors import EventAnchors
from companyk_newsbot.models import Article

T = TypeVar("T")


@dataclass(frozen=True)
class RepresentativeScore:
    source_of_record: int = 0
    direct_publisher: int = 0
    body_completeness: int = 0
    description_completeness: int = 0
    numeric_detail: int = 0
    named_factual_richness: int = 0
    aggregator_penalty: int = 0
    title_only_penalty: int = 0

    @property
    def total(self) -> int:
        return sum(self.__dict__.values())

    def payload(self) -> dict[str, int]:
        return {**self.__dict__, "total": self.total}


class RepresentativeArticleSelector:
    SOURCE_OF_RECORD = (".gov", ".go.kr", "sec.gov", "dart.fss", "fda.gov", "ema.europa.eu", "ir.", "newsroom", "investor")
    AGGREGATORS = ("news.google", "google.com", "yahoo", "msn.com", "newsbreak", "feedproxy", "feedburner")

    def source_class(self, article: Article) -> str:
        url = article.canonical_url.casefold()
        source = article.source.casefold()
        host = urlparse(url).hostname or ""
        if any(marker in host or marker in url for marker in self.SOURCE_OF_RECORD):
            return "source_of_record"
        if any(marker in host or marker in source for marker in self.AGGREGATORS) or bool(article.origin_metadata.get("redirect_url")):
            return "aggregator_or_low_information"
        return "direct_publisher" if host else "unknown_publisher"

    def score(self, article: Article) -> RepresentativeScore:
        url = article.canonical_url.casefold()
        host = urlparse(url).hostname or ""
        source_class = self.source_class(article)
        official = source_class == "source_of_record"
        aggregator = source_class == "aggregator_or_low_information"
        body = (article.text or "").strip()
        description = (article.description or "").strip()
        anchors = EventAnchors.from_article(article)
        numeric_count = len(anchors.amount_tokens | anchors.percentage_tokens | anchors.explicit_date_tokens)
        factual_count = len(anchors.counterparties | anchors.action_terms | anchors.milestone_terms)
        return RepresentativeScore(
            source_of_record=40 if official else 0,
            direct_publisher=20 if host and not official and not aggregator else 0,
            body_completeness=20 if len(body) >= 160 else (10 if len(body) >= 60 else 0),
            description_completeness=10 if len(description) >= 60 else (5 if len(description) >= 25 else 0),
            numeric_detail=min(10, numeric_count * 3),
            named_factual_richness=min(10, factual_count * 3),
            aggregator_penalty=-30 if aggregator else 0,
            title_only_penalty=-15 if not body and not description else 0,
        )

    def choose(self, values: Iterable[T], article_of: Callable[[T], Article]) -> tuple[T, tuple[T, ...], dict[str, RepresentativeScore]]:
        from companyk_newsbot.dedup.event import article_id

        members = tuple(values)
        if not members:
            raise ValueError("representative selection requires at least one article")
        scores = {article_id(article_of(value)): self.score(article_of(value)) for value in members}
        ordered = sorted(
            members,
            key=lambda value: (
                -scores[article_id(article_of(value))].total,
                -len(article_of(value).text or ""),
                -len(article_of(value).description or ""),
                article_of(value).published_at is None,
                article_of(value).published_at,
                article_of(value).canonical_url.casefold(),
                article_id(article_of(value)),
            ),
        )
        return ordered[0], tuple(ordered[1:]), scores

    def corroborating(self, representative: Article, alternatives: Iterable[Article], *, limit: int = 3) -> tuple[Article, ...]:
        from companyk_newsbot.dedup.event import article_id

        base = EventAnchors.from_article(representative).signature_tokens()
        selected: list[Article] = []
        covered = set(base)
        ordered = sorted(
            alternatives,
            key=lambda article: (-self.score(article).total, article.canonical_url.casefold(), article_id(article)),
        )
        while ordered and len(selected) < limit:
            best = max(
                ordered,
                key=lambda article: (
                    len(EventAnchors.from_article(article).signature_tokens() - covered),
                    self.score(article).total,
                    -len(article.canonical_url),
                ),
            )
            selected.append(best)
            covered.update(EventAnchors.from_article(best).signature_tokens())
            ordered.remove(best)
        return tuple(selected)
