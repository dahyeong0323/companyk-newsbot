"""Cost-first Route A-only article-to-email processing core."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from companyk_newsbot.dedup import ArticleDeduplicator, RouteAEventClusterer
from companyk_newsbot.email import EmailNewsItem
from companyk_newsbot.judges.direct_event import DirectEventAssessment, DirectGroundingVerdict
from companyk_newsbot.judges.summary import SummaryOutput
from companyk_newsbot.models import Article
from companyk_newsbot.portfolio_registry import PortfolioRegistry
from companyk_newsbot.ranking import NewsRanker, RankedNewsItem
from companyk_newsbot.rules import RouteADetector


class Judge(Protocol):
    def assess(self, event): ...


class Grounder(Protocol):
    def ground(self, event, assessment): ...


@dataclass(frozen=True)
class RouteAOnlyResult:
    deduped_articles: int
    article_duplicates: int
    matches: tuple
    events: tuple
    assessments: dict[str, DirectEventAssessment]
    ignore_count: int
    deliver_high: int
    deliver_medium: int
    ranked_items: tuple[RankedNewsItem, ...]
    grounding_verdicts: dict[str, DirectGroundingVerdict]
    email_items: tuple[EmailNewsItem, ...]


def process_route_a_articles(articles: list[Article], registry: PortfolioRegistry, *, judge: Judge, grounder: Grounder,
    ranker: NewsRanker | None = None, event_resolver=None) -> RouteAOnlyResult:
    """Deduplicate, assess, order, and ground every qualifying delivery event."""
    dedup = ArticleDeduplicator().deduplicate(articles)
    detector = RouteADetector(registry)
    scoped_articles = tuple(detector.with_candidate_provenance(article) for article in dedup.articles)
    matches = tuple(match for article in scoped_articles for match in detector.detect_scoped(article))
    events = tuple(RouteAEventClusterer(resolver=event_resolver).cluster(matches))
    assessments = {event.event_id: judge.assess(event) for event in events}
    deliver_events = [event for event in events if assessments[event.event_id].decision == "DELIVER"]
    ranked = (ranker or NewsRanker()).rank([RankedNewsItem.from_direct_event(
        event, materiality=assessments[event.event_id].materiality) for event in deliver_events])
    event_by_id = {event.event_id: event for event in deliver_events}
    verdicts: dict[str, DirectGroundingVerdict] = {}; email_items: list[EmailNewsItem] = []
    for item in ranked:
        event = event_by_id[item.event_id]; assessment = assessments[item.event_id]
        fact, insight, verdict = grounder.ground(event, assessment); verdicts[item.event_id] = verdict
        summary = SummaryOutput(fact_summary=fact, insight_one_liner=insight, insight_dimension="other",
            insight_mode="implication", confidence="medium", evidence_article_ids=assessment.evidence_article_ids)
        email_items.append(EmailNewsItem(item, summary))
    return RouteAOnlyResult(len(dedup.articles), sum(len(group.duplicates) for group in dedup.duplicate_groups),
        matches, events, assessments, sum(v.decision == "IGNORE" for v in assessments.values()),
        sum(v.decision == "DELIVER" and v.materiality == "high" for v in assessments.values()),
        sum(v.decision == "DELIVER" and v.materiality == "medium" for v in assessments.values()),
        tuple(ranked), verdicts, tuple(email_items))
