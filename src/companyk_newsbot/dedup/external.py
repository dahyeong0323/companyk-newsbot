"""Conservative Route B external-event clustering after qualification."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
from typing import Iterable

from companyk_newsbot.dedup.event import EventAnchors, article_id
from companyk_newsbot.judges.route_b import JudgedRouteBCandidate


@dataclass(frozen=True)
class ExternalEventCluster:
    event_id: str
    event_family: str
    representative: JudgedRouteBCandidate
    coverage: tuple[JudgedRouteBCandidate, ...]
    impact_links: tuple[JudgedRouteBCandidate, ...]
    anchors: EventAnchors

    @property
    def coverage_count(self) -> int:
        return 1 + len(self.coverage)

    @property
    def companies(self) -> tuple[str, ...]:
        return tuple(sorted({item.candidate.company for item in self.impact_links}, key=str.casefold))


class RouteBEventClusterer:
    """False merges are more costly than duplicate leakage."""
    def __init__(self, *, event_window_hours: int = 72) -> None:
        self.event_window = timedelta(hours=event_window_hours)

    def cluster(self, judged: Iterable[JudgedRouteBCandidate]) -> list[ExternalEventCluster]:
        groups: list[list[JudgedRouteBCandidate]] = []
        for item in sorted(judged, key=lambda x: (x.decision.event_family or "", x.candidate.article.published_at is None, x.candidate.article.published_at, x.candidate.article.canonical_url, x.candidate.company)):
            for group in groups:
                if self._same(group[0], item) and all(not EventAnchors.from_article(existing.candidate.article).conflicts_with(EventAnchors.from_article(item.candidate.article)) for existing in group):
                    group.append(item); break
            else:
                groups.append([item])
        result: list[ExternalEventCluster] = []
        for group in groups:
            ordered = sorted(group, key=lambda x: (-len(x.candidate.article.text or ""), -len(x.candidate.article.description or ""), x.candidate.article.published_at is None, x.candidate.article.published_at, x.candidate.article.canonical_url))
            representative = ordered[0]
            articles: dict[str, JudgedRouteBCandidate] = {}
            for item in ordered:
                articles.setdefault(article_id(item.candidate.article), item)
            coverage = tuple(item for item in articles.values() if item is not representative)
            event_id = hashlib.sha256(f"{representative.decision.event_family}|{'|'.join(sorted(articles))}".encode()).hexdigest()[:16]
            result.append(ExternalEventCluster(event_id, representative.decision.event_family or "unknown", representative, coverage, tuple(ordered), EventAnchors.from_article(representative.candidate.article)))
        return result

    def _same(self, left: JudgedRouteBCandidate, right: JudgedRouteBCandidate) -> bool:
        if left.decision.event_family != right.decision.event_family:
            return False
        a, b = left.candidate.article, right.candidate.article
        if a.published_at and b.published_at and abs(a.published_at - b.published_at) > self.event_window:
            return False
        aa, bb = EventAnchors.from_article(a), EventAnchors.from_article(b)
        return not aa.conflicts_with(bb) and (a.canonical_url == b.canonical_url or len(aa.tokens & bb.tokens) >= 4)
