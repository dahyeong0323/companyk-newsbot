"""Deterministic ordering and daily limits for qualified news items."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Sequence

from companyk_newsbot.judges.route_b import JudgedRouteBCandidate
from companyk_newsbot.rules import RouteAMatch


Route = Literal["direct", "external"]
Materiality = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class RankedNewsItem:
    route: Route
    company: str
    article_title: str
    article_url: str
    published_at: datetime | None
    materiality: Materiality
    direct_match: RouteAMatch | None = None
    external_match: JudgedRouteBCandidate | None = None

    @classmethod
    def from_direct(cls, match: RouteAMatch, *, materiality: Materiality = "medium") -> "RankedNewsItem":
        return cls(
            route="direct",
            company=match.company,
            article_title=match.article.title,
            article_url=match.article.canonical_url,
            published_at=match.article.published_at,
            materiality=materiality,
            direct_match=match,
        )

    @classmethod
    def from_external(cls, judged: JudgedRouteBCandidate) -> "RankedNewsItem":
        decision = judged.decision
        if not decision.qualifies or decision.materiality == "none":
            raise ValueError("only qualifying external judgments can be ranked")
        return cls(
            route="external",
            company=judged.candidate.company,
            article_title=judged.candidate.article.title,
            article_url=judged.candidate.article.canonical_url,
            published_at=judged.candidate.article.published_at,
            materiality=decision.materiality,
            external_match=judged,
        )


class NewsRanker:
    """Apply a visible priority order without hiding any threshold in routing code."""

    def __init__(self, *, total_max_items: int = 12, max_items_per_company: int = 2) -> None:
        if total_max_items < 1 or max_items_per_company < 1:
            raise ValueError("ranking limits must be positive")
        self.total_max_items = total_max_items
        self.max_items_per_company = max_items_per_company

    def rank(self, items: Sequence[RankedNewsItem]) -> list[RankedNewsItem]:
        ordered = sorted(items, key=self._sort_key)
        per_company: dict[str, int] = {}
        selected: list[RankedNewsItem] = []
        for item in ordered:
            if per_company.get(item.company, 0) >= self.max_items_per_company:
                continue
            selected.append(item)
            per_company[item.company] = per_company.get(item.company, 0) + 1
            if len(selected) == self.total_max_items:
                break
        return selected

    @staticmethod
    def _sort_key(item: RankedNewsItem) -> tuple[int, float, str, str]:
        priority = {
            ("direct", "high"): 0,
            ("external", "high"): 1,
            ("direct", "medium"): 2,
            ("direct", "low"): 3,
            ("external", "medium"): 4,
            ("external", "low"): 5,
        }[(item.route, item.materiality)]
        timestamp = item.published_at.timestamp() if item.published_at else float("-inf")
        return priority, -timestamp, item.company.casefold(), item.article_title.casefold()
