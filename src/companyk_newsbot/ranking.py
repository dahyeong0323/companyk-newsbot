"""Deterministic ordering for qualified news items, with optional legacy limits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Sequence

from companyk_newsbot.judges.route_b import JudgedRouteBCandidate
from companyk_newsbot.rules import RouteAMatch

if TYPE_CHECKING:
    from companyk_newsbot.dedup.event import EventCluster
    from companyk_newsbot.dedup.external import ExternalEventCluster


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
    impacted_companies: tuple[str, ...] = ()
    direct_event: "EventCluster | None" = None
    external_event: "ExternalEventCluster | None" = None
    event_id: str = ""
    coverage_count: int = 1

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
            impacted_companies=(match.company,),
        )

    @classmethod
    def from_direct_event(cls, event: "EventCluster", *, materiality: Materiality = "medium") -> "RankedNewsItem":
        item = cls.from_direct(event.primary, materiality=materiality)
        return cls(**{**item.__dict__, "direct_event": event, "event_id": event.event_id, "coverage_count": event.coverage_count})

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
            impacted_companies=(judged.candidate.company,),
        )

    @classmethod
    def from_external_event(cls, event: "ExternalEventCluster") -> "RankedNewsItem":
        """One external event gets one global slot while retaining all company impacts."""
        representative = event.representative
        decision = representative.decision
        companies = tuple(event.companies)
        if not companies:
            raise ValueError("external event requires an impact link")
        return cls(
            route="external", company=" · ".join(companies), article_title=representative.candidate.article.title,
            article_url=representative.candidate.article.canonical_url, published_at=representative.candidate.article.published_at,
            materiality=event.materiality, external_match=representative, impacted_companies=companies,
            external_event=event, event_id=event.event_id, coverage_count=event.coverage_count,
        )


class NewsRanker:
    """Order qualified items; optional limits exist only for explicit legacy callers."""

    def __init__(self, *, total_max_items: int | None = None, max_items_per_company: int | None = None) -> None:
        if total_max_items is not None and total_max_items < 1:
            raise ValueError("total ranking limit must be positive when set")
        if max_items_per_company is not None and max_items_per_company < 1:
            raise ValueError("per-company ranking limit must be positive when set")
        self.total_max_items = total_max_items
        self.max_items_per_company = max_items_per_company

    def rank(self, items: Sequence[RankedNewsItem]) -> list[RankedNewsItem]:
        ordered = sorted(items, key=self._sort_key)
        per_company: dict[str, int] = {}
        selected: list[RankedNewsItem] = []
        for item in ordered:
            companies = item.impacted_companies or (item.company,)
            if self.max_items_per_company is not None and any(
                per_company.get(company, 0) >= self.max_items_per_company for company in companies
            ):
                continue
            selected.append(item)
            for company in companies:
                per_company[company] = per_company.get(company, 0) + 1
            if self.total_max_items is not None and len(selected) == self.total_max_items:
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
