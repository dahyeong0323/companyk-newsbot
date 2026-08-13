"""Route B event aggregation retaining every approved impact link."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
from typing import Iterable, Literal

from companyk_newsbot.dedup.anchors import EventAnchors
from companyk_newsbot.dedup.article import normalized_title
from companyk_newsbot.dedup.event import EventDedupMetrics, PairDecision, article_id, deterministic_pair_decision
from companyk_newsbot.dedup.representative import RepresentativeArticleSelector, RepresentativeScore
from companyk_newsbot.dedup.resolver import EventPairResolver
from companyk_newsbot.judges.route_b import JudgedRouteBCandidate
from companyk_newsbot.models import Article

Materiality = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class ExternalEventCluster:
    event_id: str
    event_family: str
    source_families: tuple[str, ...]
    representative: JudgedRouteBCandidate
    coverage: tuple[JudgedRouteBCandidate, ...]
    impact_links: tuple[JudgedRouteBCandidate, ...]
    anchors: EventAnchors
    representative_scores: dict[str, RepresentativeScore]
    dedup_decisions: tuple[PairDecision, ...]
    materiality: Materiality

    @property
    def coverage_count(self) -> int:
        return 1 + len(self.coverage)

    @property
    def companies(self) -> tuple[str, ...]:
        return tuple(sorted({link.candidate.company for link in self.impact_links}, key=str.casefold))

    @property
    def all_articles(self) -> tuple[Article, ...]:
        return (self.representative.candidate.article, *(link.candidate.article for link in self.coverage))


class RouteBEventClusterer:
    def __init__(self, *, event_window_hours: int = 72, selector: RepresentativeArticleSelector | None = None, resolver: EventPairResolver | None = None) -> None:
        self.event_window = timedelta(hours=event_window_hours)
        self.selector = selector or RepresentativeArticleSelector()
        self.resolver = resolver
        self.metrics = EventDedupMetrics()

    def cluster(self, judged: Iterable[JudgedRouteBCandidate]) -> list[ExternalEventCluster]:
        self.metrics = EventDedupMetrics()
        # Exact article identity comes first, regardless of upstream family labels.
        exact_groups = self._exact_identity_groups(judged)
        groups: list[tuple[list[JudgedRouteBCandidate], list[PairDecision]]] = []
        for exact_links, exact_audits in exact_groups:
            candidate = self._best_article_link(exact_links)
            rejected_audits: list[PairDecision] = []
            for members, audits in groups:
                representative_articles = self._unique_articles(members)
                pair_audits = [self._pair(existing, candidate) for existing in representative_articles]
                if all(audit.final_decision == "SAME_EVENT" for audit in pair_audits):
                    members.extend(exact_links); audits.extend((*exact_audits, *rejected_audits, *pair_audits)); break
                rejected_audits.extend(pair_audits)
            else:
                groups.append((list(exact_links), [*exact_audits, *rejected_audits]))
        return [self._event(members, audits) for members, audits in groups]

    def _pair(self, left: JudgedRouteBCandidate, right: JudgedRouteBCandidate) -> PairDecision:
        la = EventAnchors.from_article(left.candidate.article, event_families=(left.decision.event_family,))
        ra = EventAnchors.from_article(right.candidate.article, event_families=(right.decision.event_family,))
        deterministic, reason = deterministic_pair_decision(left.candidate.article, right.candidate.article, left_anchors=la, right_anchors=ra, event_window=self.event_window)
        if deterministic == "SAME_EVENT": self.metrics.deterministic_same_event += 1
        elif deterministic == "DIFFERENT_EVENT": self.metrics.deterministic_different_event += 1
        else: self.metrics.ambiguous_pairs += 1
        if deterministic != "AMBIGUOUS" or self.resolver is None:
            return PairDecision(article_id(left.candidate.article), article_id(right.candidate.article), deterministic, reason)
        result = self.resolver.resolve(left.candidate.article, right.candidate.article)
        self.metrics.luna_event_dedup_calls += 1
        if result.failure_type: self.metrics.luna_event_dedup_failures += 1
        return PairDecision(article_id(left.candidate.article), article_id(right.candidate.article), deterministic, reason, True, result.decision, result.short_reason, result.failure_type)

    def _event(self, links: list[JudgedRouteBCandidate], audits: list[PairDecision]) -> ExternalEventCluster:
        articles = self._unique_articles(links)
        representative_link, _, scores = self.selector.choose(articles, lambda value: value.candidate.article)
        representative_id = article_id(representative_link.candidate.article)
        representative = min((link for link in links if article_id(link.candidate.article) == representative_id), key=self._link_key)
        coverage = tuple(link for link in articles if article_id(link.candidate.article) != representative_id)
        source_families = tuple(sorted({link.decision.event_family for link in links if link.decision.event_family and link.decision.event_family != "none"}))
        canonical_family = source_families[0] if source_families else "unknown"
        materiality = self._aggregate_materiality(links)
        ids = sorted({article_id(link.candidate.article) for link in links})
        event_id = hashlib.sha256(f"route_b|{'|'.join(ids)}".encode()).hexdigest()[:16]
        return ExternalEventCluster(event_id, canonical_family, source_families, representative, coverage, tuple(sorted(links, key=self._link_key)), EventAnchors.from_article(representative.candidate.article, event_families=source_families), scores, tuple(audits), materiality)

    def _exact_identity_groups(self, judged: Iterable[JudgedRouteBCandidate]) -> list[tuple[list[JudgedRouteBCandidate], tuple[PairDecision, ...]]]:
        """Union links by canonical URL OR stable normalized-title fingerprint before family checks."""
        links = sorted(judged, key=self._link_key)
        grouped = self._identity_components(links)

        output: list[tuple[list[JudgedRouteBCandidate], tuple[PairDecision, ...]]] = []
        for group in grouped:
            representative = self._best_article_link(group)
            representative_article = representative.candidate.article
            audits: list[PairDecision] = []
            seen_ids = {article_id(representative_article)}
            for link in group:
                article = link.candidate.article
                current_id = article_id(article)
                if current_id in seen_ids:
                    continue
                reason = (
                    "exact_canonical_url_identity"
                    if article.canonical_url.strip().casefold() == representative_article.canonical_url.strip().casefold()
                    else "stable_article_fingerprint_identity"
                )
                audits.append(PairDecision(article_id(representative_article), current_id, "SAME_EVENT", reason))
                self.metrics.deterministic_same_event += 1
                seen_ids.add(current_id)
            output.append((group, tuple(audits)))
        return output

    @staticmethod
    def _identity_components(links: list[JudgedRouteBCandidate]) -> list[list[JudgedRouteBCandidate]]:
        parents = list(range(len(links)))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        url_index: dict[str, int] = {}
        fingerprint_index: dict[str, int] = {}
        for index, link in enumerate(links):
            article = link.candidate.article
            url = article.canonical_url.strip().casefold()
            fingerprint = RouteBEventClusterer._stable_article_fingerprint(article)
            for key, lookup in ((url, url_index), (fingerprint, fingerprint_index)):
                if not key:
                    continue
                if key in lookup:
                    union(lookup[key], index)
                else:
                    lookup[key] = index

        grouped: dict[int, list[JudgedRouteBCandidate]] = {}
        for index, link in enumerate(links):
            grouped.setdefault(find(index), []).append(link)
        return list(grouped.values())

    @staticmethod
    def _stable_article_fingerprint(article: Article) -> str:
        title = normalized_title(article.title)
        if not title or article.published_at is None:
            return ""
        return f"{title}|{article.source.strip().casefold()}|{article.published_at.isoformat()}"

    def _best_article_link(self, links: Iterable[JudgedRouteBCandidate]) -> JudgedRouteBCandidate:
        values = list(links)
        representative, _, _ = self.selector.choose(values, lambda value: value.candidate.article)
        return representative

    @staticmethod
    def _aggregate_materiality(links: Iterable[JudgedRouteBCandidate]) -> Materiality:
        values = {link.decision.materiality for link in links}
        if "high" in values: return "high"
        if "medium" in values: return "medium"
        return "low"

    def _unique_articles(self, links: Iterable[JudgedRouteBCandidate]) -> list[JudgedRouteBCandidate]:
        groups = self._identity_components(sorted(links, key=self._link_key))
        return [self._best_article_link(group) for group in groups]

    @staticmethod
    def _link_key(link: JudgedRouteBCandidate) -> tuple[str, str, str, str, str]:
        return (article_id(link.candidate.article), link.candidate.company.casefold(), link.candidate.exposure_id, link.decision.event_family, link.decision.materiality)
