"""High-precision Route A event clustering with pair-level audit."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from difflib import SequenceMatcher
import hashlib
from typing import Iterable, Literal

from companyk_newsbot.dedup.anchors import EventAnchors
from companyk_newsbot.dedup.article import normalized_title
from companyk_newsbot.dedup.representative import RepresentativeArticleSelector, RepresentativeScore
from companyk_newsbot.dedup.resolver import EventPairResolver, ResolverResult
from companyk_newsbot.models import Article
from companyk_newsbot.rules import RouteAMatch

EventDecision = Literal["SAME_EVENT", "DIFFERENT_EVENT", "AMBIGUOUS"]


def article_id(article: Article) -> str:
    raw = f"{article.canonical_url.strip().casefold()}|{normalized_title(article.title)}|{article.published_at}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PairDecision:
    left_article_id: str
    right_article_id: str
    deterministic_decision: EventDecision
    deterministic_reason: str
    luna_invoked: bool = False
    luna_decision: str | None = None
    luna_short_reason: str | None = None
    luna_failure_type: str | None = None

    @property
    def final_decision(self) -> str:
        return self.luna_decision or self.deterministic_decision

    def payload(self) -> dict[str, object]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class EventCluster:
    company: str
    primary: RouteAMatch
    coverage: tuple[RouteAMatch, ...]
    event_id: str
    anchors: EventAnchors
    representative_scores: dict[str, RepresentativeScore]
    dedup_decisions: tuple[PairDecision, ...]

    @property
    def coverage_count(self) -> int:
        return 1 + len(self.coverage)

    @property
    def all_matches(self) -> tuple[RouteAMatch, ...]:
        return (self.primary, *self.coverage)


@dataclass
class EventDedupMetrics:
    deterministic_same_event: int = 0
    deterministic_different_event: int = 0
    ambiguous_pairs: int = 0
    luna_event_dedup_calls: int = 0
    luna_event_dedup_failures: int = 0

    def payload(self) -> dict[str, int]:
        return self.__dict__.copy()


def deterministic_pair_decision(
    left: Article,
    right: Article,
    *,
    left_anchors: EventAnchors,
    right_anchors: EventAnchors,
    event_window: timedelta,
) -> tuple[EventDecision, str]:
    if left.published_at and right.published_at and abs(left.published_at - right.published_at) > event_window:
        return "DIFFERENT_EVENT", "publication_window_conflict"
    conflict = left_anchors.conflict_reason(right_anchors)
    if conflict:
        return "DIFFERENT_EVENT", conflict
    if normalized_title(left.title) == normalized_title(right.title):
        return "SAME_EVENT", "exact_normalized_title"
    left_title, right_title = normalized_title(left.title), normalized_title(right.title)
    ratio = SequenceMatcher(None, left_title, right_title).ratio()
    if left_anchors.has_partial_distinctive_mismatch(right_anchors):
        return "AMBIGUOUS", "partial_distinctive_anchor_mismatch"
    identity_categories = left_anchors.shared_identity_categories(right_anchors)
    subject_overlap = left_anchors.subject_terms & right_anchors.subject_terms
    primary_action_shared = bool(
        left_anchors.primary_action_terms
        and left_anchors.primary_action_terms == right_anchors.primary_action_terms
    )
    milestone_shared = bool(
        left_anchors.milestone_terms
        and left_anchors.milestone_terms == right_anchors.milestone_terms
    )
    if ratio >= 0.92 and identity_categories and subject_overlap:
        return "SAME_EVENT", "high_title_similarity_with_distinctive_anchor"
    if len(identity_categories) >= 2 and primary_action_shared and subject_overlap:
        return "SAME_EVENT", "multiple_canonical_identity_anchors"
    if identity_categories and primary_action_shared and milestone_shared and subject_overlap:
        return "SAME_EVENT", "canonical_identity_action_milestone_combination"
    return "AMBIGUOUS", "insufficient_distinctive_event_identity"


class RouteAEventClusterer:
    def __init__(self, *, event_window_hours: int = 72, selector: RepresentativeArticleSelector | None = None, resolver: EventPairResolver | None = None) -> None:
        self.event_window = timedelta(hours=event_window_hours)
        self.selector = selector or RepresentativeArticleSelector()
        self.resolver = resolver
        self.metrics = EventDedupMetrics()

    def cluster(self, matches: Iterable[RouteAMatch]) -> list[EventCluster]:
        self.metrics = EventDedupMetrics()
        groups: list[tuple[list[RouteAMatch], list[PairDecision]]] = []
        ordered = sorted(matches, key=lambda value: (value.company.casefold(), value.article.published_at is None, value.article.published_at, value.article.canonical_url.casefold(), article_id(value.article)))
        for match in ordered:
            rejected_audits: list[PairDecision] = []
            for members, audits in groups:
                if members[0].company != match.company:
                    continue
                pair_audits = [self._pair(existing.article, match.article) for existing in members]
                if all(audit.final_decision == "SAME_EVENT" for audit in pair_audits):
                    members.append(match)
                    audits.extend((*rejected_audits, *pair_audits))
                    break
                rejected_audits.extend(pair_audits)
            else:
                groups.append(([match], rejected_audits))
        return [self._event(members, audits) for members, audits in groups]

    def _pair(self, left: Article, right: Article) -> PairDecision:
        deterministic, reason = deterministic_pair_decision(
            left, right,
            left_anchors=EventAnchors.from_article(left),
            right_anchors=EventAnchors.from_article(right),
            event_window=self.event_window,
        )
        if deterministic == "SAME_EVENT": self.metrics.deterministic_same_event += 1
        elif deterministic == "DIFFERENT_EVENT": self.metrics.deterministic_different_event += 1
        else: self.metrics.ambiguous_pairs += 1
        if deterministic != "AMBIGUOUS" or self.resolver is None:
            return PairDecision(article_id(left), article_id(right), deterministic, reason)
        result: ResolverResult = self.resolver.resolve(left, right)
        self.metrics.luna_event_dedup_calls += 1
        if result.failure_type: self.metrics.luna_event_dedup_failures += 1
        return PairDecision(article_id(left), article_id(right), deterministic, reason, True, result.decision, result.short_reason, result.failure_type)

    def _event(self, members: list[RouteAMatch], audits: list[PairDecision]) -> EventCluster:
        primary, coverage, scores = self.selector.choose(members, lambda value: value.article)
        ids = sorted(article_id(member.article) for member in members)
        event_id = hashlib.sha256(f"route_a|{members[0].company}|{'|'.join(ids)}".encode()).hexdigest()[:16]
        return EventCluster(members[0].company, primary, coverage, event_id, EventAnchors.from_article(primary.article), scores, tuple(audits))
