"""Real, test-recipient-only end-to-end execution for the newsbot."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
import asyncio
import hashlib
import json
import os
from zoneinfo import ZoneInfo

from companyk_newsbot.collectors.google_news_rss import GoogleNewsRSSCollector
from companyk_newsbot.config import KeywordMapConfig
from companyk_newsbot.dedup import ArticleDeduplicator, RouteAEventClusterer
from companyk_newsbot.email import EmailNewsItem, HtmlEmailRenderer, ResendEmailSender, ResendSettings
from companyk_newsbot.judges import NewsSummarizer, RouteBCausalMaterialityJudge
from companyk_newsbot.ranking import NewsRanker, RankedNewsItem
from companyk_newsbot.rules import ExposureRegistry, RouteADetector, RouteBCandidateGenerator
from companyk_newsbot.state import JsonStateStore


TEST_RECIPIENT = "jeremy.cheon@pm.me"
KST = ZoneInfo("Asia/Seoul")
MAX_JUDGE_CALLS = 25
MAX_DIRECT_QUERIES = 20
MAX_EXPOSURE_QUERIES = 20


class E2EExecutionError(RuntimeError):
    """Adds a clear pipeline-stage boundary to a real E2E failure."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"E2E failed at {stage}: {message}")
        self.stage = stage


@dataclass(frozen=True)
class E2EResult:
    collected: int
    collection_failures: int
    article_deduped: int
    article_duplicates: int
    route_a_matches: int
    route_a_events: int
    route_b_candidates: int
    route_b_accepted: int
    route_b_rejected: int
    reject_reasons: dict[str, int]
    final_items: int
    already_sent: int
    judge_calls: int
    summary_calls: int
    delivery_id: str

    def log_payload(self) -> dict[str, object]:
        return self.__dict__.copy()


def _fingerprint(item: RankedNewsItem) -> tuple[str, str]:
    kind = "event" if item.route == "direct" else "article"
    value = "|".join((item.route, item.company, item.article_url, item.article_title))
    return kind, hashlib.sha256(value.encode("utf-8")).hexdigest()


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True))


def run_real_e2e(config: KeywordMapConfig, store: JsonStateStore, *, today: date | None = None) -> E2EResult:
    """Run the real data path and deliver only to the fixed test recipient."""
    report_date = today or date.today()
    settings = ResendSettings.from_environment()
    if settings.recipient.casefold() != TEST_RECIPIENT:
        raise E2EExecutionError("safety_check", f"e2e_test may send only to {TEST_RECIPIENT}")

    registry = ExposureRegistry(config)
    direct_queries = tuple(config.company_rules)[:MAX_DIRECT_QUERIES]
    exposure_queries = tuple(query.query for query in registry.queries)[:MAX_EXPOSURE_QUERIES]
    _log(
        "queries_prepared",
        direct=len(direct_queries),
        external=len(exposure_queries),
        direct_cap=MAX_DIRECT_QUERIES,
        external_cap=MAX_EXPOSURE_QUERIES,
    )

    collected = []
    collection_failures: list[dict[str, str]] = []
    try:
        async def collect_all() -> list:
            async with GoogleNewsRSSCollector() as collector:
                direct_result = await collector.collect_many(direct_queries)
                external_result = await collector.collect_many(exposure_queries)
                for route, result in (("direct", direct_result), ("external", external_result)):
                    _log(
                        "collection_route",
                        route=route,
                        queries=len(result.queries),
                        successes=len(result.successes),
                        failures=len(result.failures),
                        articles=len(result.articles),
                    )
                    for failure in result.failures:
                        failure_record = {
                            "route": route,
                            "query": failure.query,
                            "status": failure.status,
                            "reason": failure.error or failure.status,
                        }
                        collection_failures.append(failure_record)
                        _log("collection_query_failed", **failure_record)
                return [*direct_result.articles, *external_result.articles]

        collected = asyncio.run(collect_all())
    except Exception as exc:
        raise E2EExecutionError("collection", str(exc)) from exc
    collected_today = [
        article
        for article in collected
        if article.published_at is not None and article.published_at.astimezone(KST).date() == report_date
    ]
    _log("collection_complete", collected=len(collected), same_day=len(collected_today), failures=len(collection_failures))

    try:
        article_dedup = ArticleDeduplicator().deduplicate(collected_today)
        detector = RouteADetector(config)
        route_a_matches = [match for article in article_dedup.articles for match in detector.detect(article)]
        route_a_events = RouteAEventClusterer().cluster(route_a_matches)
        candidate_result = RouteBCandidateGenerator(registry).generate(article_dedup.articles)
    except Exception as exc:
        raise E2EExecutionError("deterministic_routing", str(exc)) from exc

    reasons = Counter(rejection.reason for rejection in candidate_result.rejections)
    _log(
        "deterministic_complete",
        article_deduped=len(article_dedup.articles),
        article_duplicates=sum(len(group.duplicates) for group in article_dedup.duplicate_groups),
        route_a_matches=len(route_a_matches),
        route_a_events=len(route_a_events),
        route_b_candidates=len(candidate_result.candidates),
        route_b_reject_reasons=dict(reasons),
    )

    judge = RouteBCausalMaterialityJudge.from_environment()
    judged = []
    try:
        candidates_to_judge = candidate_result.candidates[:MAX_JUDGE_CALLS]
        skipped_for_cap = candidate_result.candidates[MAX_JUDGE_CALLS:]
        if skipped_for_cap:
            reasons["e2e_judge_cap"] += len(skipped_for_cap)
            _log("route_b_judge_cap", max_calls=MAX_JUDGE_CALLS, skipped=len(skipped_for_cap))
        for candidate in candidates_to_judge:
            result = judge.judge(candidate)
            judged.append(result)
            if not result.decision.qualifies:
                reasons[f"judge:{result.decision.rejection_reason}"] += 1
    except Exception as exc:
        raise E2EExecutionError("route_b_openai_judge", str(exc)) from exc

    accepted = [result for result in judged if result.decision.qualifies]
    ranked = NewsRanker().rank(
        [*(RankedNewsItem.from_direct(event.primary) for event in route_a_events), *(RankedNewsItem.from_external(result) for result in accepted)]
    )
    unsent: list[RankedNewsItem] = []
    already_sent = 0
    for item in ranked:
        kind, fingerprint = _fingerprint(item)
        if store.was_sent(fingerprint, kind=kind):
            already_sent += 1
        else:
            unsent.append(item)
    _log("ranking_complete", ranked=len(ranked), unsent=len(unsent), already_sent=already_sent)

    try:
        from openai import OpenAI

        summarizer = NewsSummarizer(
            OpenAI(),
            model=os.getenv("OPENAI_MODEL", "gpt-5.6-sol"),
            reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "medium"),
        )
        email_items = [EmailNewsItem(item, summarizer.summarize(item)) for item in unsent]
        rendered = HtmlEmailRenderer().render(email_items, report_date=report_date)
    except Exception as exc:
        raise E2EExecutionError("openai_summary_or_email_render", str(exc)) from exc

    try:
        sender = ResendEmailSender(settings)
        try:
            delivery_id = sender.send(rendered)
        finally:
            sender.close()
    except Exception as exc:
        raise E2EExecutionError("resend_delivery", str(exc)) from exc

    for item in unsent:
        kind, fingerprint = _fingerprint(item)
        store.mark_sent(fingerprint, kind=kind)
    result = E2EResult(
        collected=len(collected), collection_failures=len(collection_failures), article_deduped=len(article_dedup.articles),
        article_duplicates=sum(len(group.duplicates) for group in article_dedup.duplicate_groups),
        route_a_matches=len(route_a_matches), route_a_events=len(route_a_events), route_b_candidates=len(candidate_result.candidates),
        route_b_accepted=len(accepted), route_b_rejected=len(candidate_result.rejections) + len(judged) - len(accepted) + len(skipped_for_cap),
        reject_reasons=dict(reasons), final_items=len(unsent), already_sent=already_sent,
        judge_calls=len(judged), summary_calls=len(email_items), delivery_id=delivery_id,
    )
    _log("e2e_complete", **result.log_payload())
    return result
