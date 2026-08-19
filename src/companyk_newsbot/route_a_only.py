"""Cost-first Route A-only article-to-email processing core."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import os
from typing import Callable, Protocol

from companyk_newsbot.dedup import ArticleDeduplicator, RouteAEventClusterer
from companyk_newsbot.model_first import prepare_events
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
    model_failure_events: int = 0
    systemic_model_failure: bool = False
    model_metrics: dict[str, object] | None = None


def _systemic_model_failure(events: tuple, failures: int) -> bool:
    if not events:
        return False
    minimum = int(os.getenv("DIRECT_EVENT_SYSTEMIC_FAILURE_MIN_EVENTS", "3"))
    ratio = float(os.getenv("DIRECT_EVENT_SYSTEMIC_FAILURE_RATIO", "0.25"))
    return failures >= minimum and failures / len(events) >= ratio


def process_route_a_articles(articles: list[Article], registry: PortfolioRegistry, *, judge: Judge, grounder: Grounder,
    ranker: NewsRanker | None = None, event_resolver=None, identity_provider=None, grouping_provider=None,
    forensic_progress: Callable[[str, str, str | None], None] | None = None) -> RouteAOnlyResult:
    """Deduplicate, assess, order, and ground every qualifying delivery event."""
    dedup = ArticleDeduplicator().deduplicate(articles)
    detector = RouteADetector(registry)
    scoped_articles = tuple(detector.with_candidate_provenance(article) for article in dedup.articles)
    matches = tuple(match for article in scoped_articles for match in detector.detect_scoped(article))
    model_metrics: dict[str, object] = {}
    if identity_provider is not None and grouping_provider is not None:
        events, model_metrics = prepare_events(matches, registry, identity_provider=identity_provider,
            grouping_provider=grouping_provider, progress=forensic_progress)
    else:
        events = tuple(RouteAEventClusterer(resolver=event_resolver).cluster(matches))
    if model_metrics.get("model_first_systemic_failure"):
        return RouteAOnlyResult(len(dedup.articles), sum(len(group.duplicates) for group in dedup.duplicate_groups),
            matches, events, {}, 0, 0, 0, (), {}, (), 0, True, model_metrics)
    assessments: dict[str, DirectEventAssessment] = {}
    workers = max(1, int(os.getenv("DIRECT_EVENT_CONCURRENCY", "6")))
    def assess(event):
        if forensic_progress: forensic_progress("materiality", "started", event.event_id)
        try:
            result = event.event_id, judge.assess(event)
            if forensic_progress: forensic_progress("materiality", "completed", event.event_id)
            return result
        except Exception:
            if forensic_progress: forensic_progress("materiality", "failed", event.event_id)
            return event.event_id, DirectEventAssessment(decision="IGNORE", reason_code="model_assessment_failure", materiality="none", event_family="other", fact_summary=None, investor_insight=None, evidence_article_ids=[])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(assess, event) for event in events]):
            event_id, assessment = future.result(); assessments[event_id] = assessment
    model_failures = sum(assessment.reason_code == "model_assessment_failure" for assessment in assessments.values())
    company_counts = model_metrics.get("company_stage_counts")
    if isinstance(company_counts, list):
        by_company = {str(row.get("company")): row for row in company_counts if isinstance(row, dict)}
        for event in events:
            row = by_company.get(event.company)
            if row is None:
                continue
            decision = assessments[event.event_id].decision.casefold()
            row[decision] = int(row.get(decision, 0) or 0) + 1
    if _systemic_model_failure(events, model_failures):
        return RouteAOnlyResult(len(dedup.articles), sum(len(group.duplicates) for group in dedup.duplicate_groups),
            matches, events, assessments, sum(value.decision == "IGNORE" for value in assessments.values()),
            0, 0, (), {}, (), model_failures, True, model_metrics)
    deliver_events = [event for event in events if assessments[event.event_id].decision == "DELIVER"]
    ranked = (ranker or NewsRanker()).rank([RankedNewsItem.from_direct_event(
        event, materiality=assessments[event.event_id].materiality) for event in deliver_events])
    event_by_id = {event.event_id: event for event in deliver_events}
    verdicts: dict[str, DirectGroundingVerdict] = {}; email_items: list[EmailNewsItem] = []
    grounded: dict[str, tuple[str, str | None, DirectGroundingVerdict]] = {}
    grounding_failures = 0
    def ground(item):
        event = event_by_id[item.event_id]; assessment = assessments[item.event_id]
        if forensic_progress: forensic_progress("grounding", "started", event.event_id)
        try:
            result = item, grounder.ground(event, assessment)
            if forensic_progress: forensic_progress("grounding", "completed", event.event_id)
            return result
        except Exception:
            if forensic_progress: forensic_progress("grounding", "failed", event.event_id)
            return item, None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(ground, item) for item in ranked]
        for future in as_completed(futures):
            item, result = future.result()
            if result is None:
                grounding_failures += 1
                continue
            fact, insight, verdict = result; verdicts[item.event_id] = verdict; grounded[item.event_id] = result
    total_failures = model_failures + grounding_failures
    if _systemic_model_failure(events, total_failures):
        return RouteAOnlyResult(len(dedup.articles), sum(len(group.duplicates) for group in dedup.duplicate_groups),
            matches, events, assessments, sum(value.decision == "IGNORE" for value in assessments.values()),
            0, 0, (), verdicts, (), total_failures, True, model_metrics)
    for item in ranked:
        result = grounded.get(item.event_id)
        if result is None:
            continue
        fact, insight, _ = result; assessment = assessments[item.event_id]
        summary = SummaryOutput(fact_summary=fact, insight_one_liner=insight, insight_dimension="other",
            insight_mode="implication", confidence="medium", evidence_article_ids=assessment.evidence_article_ids)
        email_items.append(EmailNewsItem(item, summary))
    return RouteAOnlyResult(len(dedup.articles), sum(len(group.duplicates) for group in dedup.duplicate_groups),
        matches, events, assessments, sum(v.decision == "IGNORE" for v in assessments.values()),
        sum(v.decision == "DELIVER" and v.materiality == "high" for v in assessments.values()),
        sum(v.decision == "DELIVER" and v.materiality == "medium" for v in assessments.values()),
        tuple(item.item for item in email_items), verdicts, tuple(email_items), total_failures, False, model_metrics)
