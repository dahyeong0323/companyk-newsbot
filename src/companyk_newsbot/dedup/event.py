"""Initial same-company event clustering for cross-publication coverage."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Iterable

from companyk_newsbot.dedup.article import normalized_title
from companyk_newsbot.rules import RouteAMatch


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+|[가-힣]+", normalized_title(value)))


def _numbers(value: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?(?:[a-z]+|[가-힣]+)?", normalized_title(value)))


@dataclass(frozen=True)
class EventCluster:
    company: str
    primary: RouteAMatch
    coverage: tuple[RouteAMatch, ...]

    @property
    def coverage_count(self) -> int:
        return 1 + len(self.coverage)


class RouteAEventClusterer:
    """Group strongly similar direct-company articles without mixing companies/events."""

    def __init__(self, *, min_token_similarity: float = 0.5, min_title_ratio: float = 0.72) -> None:
        self.min_token_similarity = min_token_similarity
        self.min_title_ratio = min_title_ratio

    def cluster(self, matches: Iterable[RouteAMatch]) -> list[EventCluster]:
        clusters: list[EventCluster] = []
        for match in matches:
            cluster_index = next(
                (
                    index
                    for index, cluster in enumerate(clusters)
                    if cluster.company == match.company and self._same_event(cluster.primary, match)
                ),
                None,
            )
            if cluster_index is None:
                clusters.append(EventCluster(company=match.company, primary=match, coverage=()))
                continue
            cluster = clusters[cluster_index]
            primary, coverage = self._choose_primary(cluster.primary, (*cluster.coverage, match))
            clusters[cluster_index] = EventCluster(company=cluster.company, primary=primary, coverage=coverage)
        return clusters

    def _same_event(self, left: RouteAMatch, right: RouteAMatch) -> bool:
        left_title = normalized_title(left.article.title)
        right_title = normalized_title(right.article.title)
        if left_title == right_title:
            return True
        left_numbers, right_numbers = _numbers(left_title), _numbers(right_title)
        if left_numbers and right_numbers and left_numbers != right_numbers:
            return False
        left_tokens = _tokens(left_title).difference(_tokens(" ".join((left.company, *left.matched_terms))))
        right_tokens = _tokens(right_title).difference(_tokens(" ".join((right.company, *right.matched_terms))))
        if not left_tokens or not right_tokens:
            return False
        similarity = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        ratio = SequenceMatcher(None, left_title, right_title).ratio()
        return similarity >= self.min_token_similarity or ratio >= self.min_title_ratio

    @staticmethod
    def _choose_primary(primary: RouteAMatch, candidates: tuple[RouteAMatch, ...]) -> tuple[RouteAMatch, tuple[RouteAMatch, ...]]:
        all_matches = (primary, *candidates)
        ordered = sorted(
            all_matches,
            key=lambda match: (
                match.article.published_at is None,
                match.article.published_at,
                match.article.canonical_url,
            ),
        )
        return ordered[0], tuple(ordered[1:])
