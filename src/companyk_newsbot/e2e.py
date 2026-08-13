"""Real smoke and non-delivery full-shadow execution for the newsbot."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
import asyncio
import hashlib
import json
import os
from time import monotonic
from typing import Literal
from pathlib import Path
from zoneinfo import ZoneInfo

from companyk_newsbot.collectors.google_news_rss import GoogleNewsRSSCollector, normalized_query
from companyk_newsbot.config import KeywordMapConfig
from companyk_newsbot.dedup import ArticleDeduplicator, LunaEventPairResolver, RepresentativeArticleSelector, RouteAEventClusterer, RouteBEventClusterer
from companyk_newsbot.email import EmailNewsItem, HtmlEmailRenderer, ResendEmailSender, ResendSettings
from companyk_newsbot.freshness import delivery_window, filter_articles, smoke_window
from companyk_newsbot.full_shadow_artifacts import FullShadowArtifactJournal, journal_collection_data, journal_event_data, journal_qualification_data, journal_ranking_data, preflight_artifact_dir, write_full_shadow_artifacts
from companyk_newsbot.judges import InsightGroundingVerifier, NewsSummarizer, RouteBCascadeJudge, RouteBCausalMaterialityJudge
from companyk_newsbot.ranking import NewsRanker, RankedNewsItem
from companyk_newsbot.rules import ExposureRegistry, RouteADetector, RouteBCandidateGenerator
from companyk_newsbot.state import JsonStateStore


TEST_RECIPIENT = "jeremy.cheon@pm.me"
KST = ZoneInfo("Asia/Seoul")
DEFAULT_SMOKE_DIRECT_QUERY_CAP = 8
DEFAULT_SMOKE_EXPOSURE_QUERY_CAP = 8
SMOKE_MAX_JUDGE_CALLS = 25
DEFAULT_OPENAI_TIMEOUT_SECONDS = 60.0
DEFAULT_SMOKE_LOOKBACK_DAYS = 7
DEFAULT_FIRST_RUN_HOURS = 30
DEFAULT_OVERLAP_HOURS = 2
ExecutionProfile = Literal["smoke", "full_shadow"]


class E2EExecutionError(RuntimeError):
    """Adds a clear pipeline-stage boundary to a real execution failure."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"E2E failed at {stage}: {message}")
        self.stage = stage


@dataclass(frozen=True)
class E2EQueryPlan:
    profile: ExecutionProfile
    direct_queries: tuple[str, ...]
    exposure_queries: tuple[str, ...]
    queries: tuple[str, ...]


@dataclass(frozen=True)
class E2EResult:
    status: Literal["success", "inconclusive"]
    profile: ExecutionProfile
    query_count: int
    direct_query_count: int
    exposure_query_count: int
    collection_successes: int
    collection_failures: int
    collection_seconds: float
    collected: int
    freshness_seconds: float
    freshness_window_start: str
    freshness_window_end: str
    freshness_mode: str
    freshness_accepted: int
    freshness_rejected_too_old: int
    freshness_rejected_future: int
    freshness_rejected_missing_timestamp: int
    dedup_seconds: float
    article_deduped: int
    article_duplicates: int
    routing_seconds: float
    route_a_matches: int
    route_a_events: int
    route_b_candidates: int
    route_b_accepted: int
    route_b_rejected: int
    reject_reasons: dict[str, int]
    final_items: int
    already_sent: int
    same_run_duplicates: int
    openai_model: str
    judge_seconds: float
    judge_calls: int
    cascade_metrics: dict[str, object]
    summary_seconds: float
    summary_calls: int
    render_seconds: float
    email_seconds: float
    total_seconds: float
    delivery_id: str | None
    artifact_json_path: str | None
    artifact_html_path: str | None
    production_delivery_checkpoint_before: str | None

    def log_payload(self) -> dict[str, object]:
        return self.__dict__.copy()


def _seconds(started_at: float) -> float:
    return round(monotonic() - started_at, 3)


def _positive_int_from_environment(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise E2EExecutionError("configuration", f"{name} must be a positive integer") from exc
    if value < 1:
        raise E2EExecutionError("configuration", f"{name} must be a positive integer")
    return value


def _deterministic_sample(values: tuple[str, ...], cap: int, *, namespace: str) -> tuple[str, ...]:
    """Select a stable, order-independent slice instead of the YAML's first entries."""
    if len(values) <= cap:
        return values
    return tuple(
        sorted(
            values,
            key=lambda value: (
                hashlib.sha256(f"{namespace}|{normalized_query(value)}".encode("utf-8")).hexdigest(),
                normalized_query(value),
            ),
        )[:cap]
    )


def build_query_plan(
    config: KeywordMapConfig,
    *,
    profile: ExecutionProfile,
    direct_cap: int | None = None,
    exposure_cap: int | None = None,
) -> E2EQueryPlan:
    """Build smoke samples or full coverage, then de-duplicate requests across both routes."""
    registry = ExposureRegistry(config)
    all_direct = tuple(config.company_rules)
    all_exposure = tuple(query.query for query in registry.queries)
    if profile == "smoke":
        direct_limit = (
            direct_cap
            if direct_cap is not None
            else _positive_int_from_environment("E2E_DIRECT_QUERY_CAP", DEFAULT_SMOKE_DIRECT_QUERY_CAP)
        )
        exposure_limit = (
            exposure_cap
            if exposure_cap is not None
            else _positive_int_from_environment("E2E_EXPOSURE_QUERY_CAP", DEFAULT_SMOKE_EXPOSURE_QUERY_CAP)
        )
        if direct_limit < 1 or exposure_limit < 1:
            raise E2EExecutionError("configuration", "smoke query caps must be positive")
        direct_queries = _deterministic_sample(all_direct, direct_limit, namespace="route_a")
        exposure_queries = _deterministic_sample(all_exposure, exposure_limit, namespace="route_b")
    elif profile == "full_shadow":
        direct_queries = all_direct
        exposure_queries = all_exposure
    else:  # pragma: no cover - guarded by the type and main entry point
        raise E2EExecutionError("configuration", f"unknown execution profile: {profile}")

    queries: list[str] = []
    seen: set[str] = set()
    for query in (*direct_queries, *exposure_queries):
        key = normalized_query(query)
        if key not in seen:
            seen.add(key)
            queries.append(query)
    return E2EQueryPlan(profile, direct_queries, exposure_queries, tuple(queries))


def _assert_test_recipient(settings: ResendSettings) -> None:
    if settings.recipient.casefold() != TEST_RECIPIENT:
        raise E2EExecutionError("safety_check", f"smoke E2E may send only to {TEST_RECIPIENT}")


def _openai_timeout_seconds() -> float:
    raw = os.getenv("OPENAI_TIMEOUT_SECONDS", str(DEFAULT_OPENAI_TIMEOUT_SECONDS)).strip()
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise E2EExecutionError("configuration", "OPENAI_TIMEOUT_SECONDS must be positive") from exc
    if timeout <= 0:
        raise E2EExecutionError("configuration", "OPENAI_TIMEOUT_SECONDS must be positive")
    return timeout


def _fingerprint(item: RankedNewsItem) -> tuple[str, str]:
    kind = "event" if item.route == "direct" else "article"
    value = "|".join((item.route, item.company, item.article_url, item.article_title))
    return kind, hashlib.sha256(value.encode("utf-8")).hexdigest()


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True))


def run_real_e2e(
    config: KeywordMapConfig,
    store: JsonStateStore,
    *,
    now: datetime | None = None,
    profile: ExecutionProfile = "smoke",
    deliver: bool = True,
) -> E2EResult:
    """Run real services; only smoke may deliver, and only to the fixed test recipient."""
    total_started = monotonic()
    run_time = (now or datetime.now(UTC)).astimezone(UTC)
    report_date = run_time.astimezone(KST).date()
    if deliver and profile != "smoke":
        raise E2EExecutionError("safety_check", "full_shadow is a non-delivery profile")
    settings = ResendSettings.from_environment() if deliver else None
    if settings is not None:
        _assert_test_recipient(settings)

    artifact_journal: FullShadowArtifactJournal | None = None
    if profile == "full_shadow":
        try:
            artifact_dir = preflight_artifact_dir(os.getenv("ARTIFACT_DIR", ""))
            artifact_journal = FullShadowArtifactJournal(artifact_dir, run_time)
        except Exception as exc:
            raise E2EExecutionError("artifact_storage", str(exc)) from exc

    registry = ExposureRegistry(config)
    query_plan = build_query_plan(config, profile=profile)
    delivery_checkpoint_before = store.last_delivery_datetime()
    if profile == "smoke":
        smoke_days = _positive_int_from_environment("E2E_SMOKE_LOOKBACK_DAYS", DEFAULT_SMOKE_LOOKBACK_DAYS)
        freshness_window = smoke_window(now=run_time, lookback_days=smoke_days)
        freshness_hint = f"when:{smoke_days}d"
    else:
        first_run_hours = _positive_int_from_environment("FRESHNESS_FIRST_RUN_HOURS", DEFAULT_FIRST_RUN_HOURS)
        overlap_hours = _positive_int_from_environment("FRESHNESS_OVERLAP_HOURS", DEFAULT_OVERLAP_HOURS)
        freshness_window = delivery_window(
            now=run_time,
            last_successful_delivery_run=store.last_delivery_datetime(),
            overlap_hours=overlap_hours,
            first_run_hours=first_run_hours,
        )
        freshness_hint = "when:2d"
    _log(
        "queries_prepared",
        profile=profile,
        direct=len(query_plan.direct_queries),
        external=len(query_plan.exposure_queries),
        query_count=len(query_plan.queries),
    )

    collection_started = monotonic()
    try:
        async def collect_all():
            async with GoogleNewsRSSCollector(freshness_hint=freshness_hint) as collector:
                return await collector.collect_many(query_plan.queries)

        collection_result = asyncio.run(collect_all())
    except Exception as exc:
        if artifact_journal:
            artifact_journal.fail(f"collection: {exc}")
        raise E2EExecutionError("collection", str(exc)) from exc
    collection_seconds = _seconds(collection_started)
    for failure in collection_result.failures:
        _log(
            "collection_query_failed",
            query=failure.query,
            status=failure.status,
            reason=failure.error or failure.status,
        )
    collected = list(collection_result.articles)
    if artifact_journal:
        artifact_journal.update("collection", journal_collection_data(
            collected,
            query_count=len(query_plan.queries),
            collection_successes=len(collection_result.successes),
            collection_failures=len(collection_result.failures),
            collected=len(collected),
            freshness_accepted=None,
        ))
    freshness_started = monotonic()
    try:
        freshness = filter_articles(collected, window=freshness_window)
    except Exception as exc:
        if artifact_journal:
            artifact_journal.fail(f"freshness: {exc}")
        raise E2EExecutionError("freshness", str(exc)) from exc
    freshness_seconds = _seconds(freshness_started)
    fresh_articles = list(freshness.accepted)
    if artifact_journal:
        artifact_journal.update("collection", journal_collection_data(
            collected,
            query_count=len(query_plan.queries),
            collection_successes=len(collection_result.successes),
            collection_failures=len(collection_result.failures),
            collected=len(collected),
            freshness_accepted=len(fresh_articles),
        ))
    _log(
        "collection_complete",
        query_count=len(query_plan.queries),
        collection_seconds=collection_seconds,
        collection_successes=len(collection_result.successes),
        collection_failures=len(collection_result.failures),
        articles_collected=len(collected),
        freshness_window_start=freshness.window.start.isoformat(),
        freshness_window_end=freshness.window.end.isoformat(),
        freshness_mode=freshness.window.mode,
        freshness_seconds=freshness_seconds,
        freshness_accepted=len(fresh_articles),
        freshness_rejected_too_old=freshness.rejected_too_old,
        freshness_rejected_future=freshness.rejected_future,
        freshness_rejected_missing_timestamp=freshness.rejected_missing_timestamp,
    )

    dedup_started = monotonic()
    try:
        article_dedup = ArticleDeduplicator().deduplicate(fresh_articles)
    except Exception as exc:
        if artifact_journal:
            artifact_journal.fail(f"article_dedup: {exc}")
        raise E2EExecutionError("article_dedup", str(exc)) from exc
    dedup_seconds = _seconds(dedup_started)
    article_duplicates = sum(len(group.duplicates) for group in article_dedup.duplicate_groups)
    _log(
        "dedup_complete",
        dedup_seconds=dedup_seconds,
        article_deduped=len(article_dedup.articles),
        article_duplicates=article_duplicates,
    )

    routing_started = monotonic()
    try:
        detector = RouteADetector(config)
        event_resolver = LunaEventPairResolver.from_environment() if profile == "full_shadow" else None
        route_a_matches = [match for article in article_dedup.articles for match in detector.detect(article)]
        route_a_clusterer = RouteAEventClusterer(resolver=event_resolver)
        route_a_events = route_a_clusterer.cluster(route_a_matches)
        candidate_result = RouteBCandidateGenerator(registry).generate(article_dedup.articles)
    except Exception as exc:
        if artifact_journal:
            artifact_journal.fail(f"deterministic_routing: {exc}")
        raise E2EExecutionError("deterministic_routing", str(exc)) from exc
    routing_seconds = _seconds(routing_started)
    reasons = Counter(rejection.reason for rejection in candidate_result.rejections)
    _log(
        "routing_complete",
        routing_seconds=routing_seconds,
        route_a_matches=len(route_a_matches),
        route_a_events=len(route_a_events),
        route_b_candidates=len(candidate_result.candidates),
        route_b_prefilter_rejections=len(candidate_result.rejections),
        route_b_reject_reasons=dict(reasons),
    )

    judge_started = monotonic()
    judged = []
    cascade_metrics: dict[str, object] = {}
    openai_model = os.getenv("ROUTE_B_PRIMARY_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.6-sol")).strip()
    try:
        if profile == "smoke":
            candidates_to_judge = candidate_result.candidates[:SMOKE_MAX_JUDGE_CALLS]
            skipped_for_cap = candidate_result.candidates[SMOKE_MAX_JUDGE_CALLS:]
        else:
            candidates_to_judge = candidate_result.candidates
            skipped_for_cap = ()
        if skipped_for_cap:
            reasons["smoke_judge_cap"] += len(skipped_for_cap)
            _log("route_b_judge_cap", max_calls=SMOKE_MAX_JUDGE_CALLS, skipped=len(skipped_for_cap))
        if profile == "full_shadow":
            cascade = RouteBCascadeJudge.from_environment()
            judged = cascade.judge_all_sync(candidates_to_judge)
            cascade_metrics = cascade.metrics.payload()
            for result in judged:
                if result.audit.get("final_decision_source") == "unresolved":
                    reasons["judge:unresolved"] += 1
                elif not result.decision.qualifies:
                    reasons[f"judge:{result.decision.rejection_reason}"] += 1
        else:
            judge = RouteBCausalMaterialityJudge.from_environment(timeout=_openai_timeout_seconds())
            for candidate in candidates_to_judge:
                result = judge.judge(candidate)
                judged.append(result)
                if not result.decision.qualifies:
                    reasons[f"judge:{result.decision.rejection_reason}"] += 1
    except Exception as exc:
        if artifact_journal:
            artifact_journal.fail(f"route_b_openai_judge: {exc}")
        raise E2EExecutionError("route_b_openai_judge", str(exc)) from exc
    judge_seconds = _seconds(judge_started)
    accepted = [result for result in judged if result.decision.qualifies]
    if artifact_journal:
        artifact_journal.update("qualification", journal_qualification_data(judged, candidate_result.rejections))
    _log(
        "judge_complete",
        openai_model=openai_model,
        judge_seconds=judge_seconds,
        judge_calls=len(judged),
        route_b_accepted=len(accepted),
        route_b_judge_rejected=len(judged) - len(accepted),
        **cascade_metrics,
    )

    route_b_clusterer = RouteBEventClusterer(resolver=event_resolver)
    try:
        route_b_events = route_b_clusterer.cluster(accepted)
    except Exception as exc:
        if artifact_journal:
            artifact_journal.fail(f"route_b_event_dedup: {exc}")
        raise E2EExecutionError("route_b_event_dedup", str(exc)) from exc
    if artifact_journal:
        artifact_journal.update("event_dedup", journal_event_data(
            route_a_events,
            route_b_events,
            route_a_events_before_dedup=len(route_a_matches),
            route_a_events_after_dedup=len(route_a_events),
            route_b_events_before_dedup=len({result.candidate.article.canonical_url for result in accepted}),
            route_b_events_after_dedup=len(route_b_events),
            **{f"route_a_{key}": value for key, value in route_a_clusterer.metrics.payload().items()},
            **{f"route_b_{key}": value for key, value in route_b_clusterer.metrics.payload().items()},
        ))
    event_articles_before = len(route_a_matches) + len({result.candidate.article.canonical_url for result in accepted})
    event_count_after = len(route_a_events) + len(route_b_events)
    collapsed_events = max(0, event_articles_before - event_count_after)
    representative_selector = RepresentativeArticleSelector()
    representative_distribution = Counter(
        representative_selector.source_class(article)
        for article in (
            *(event.primary.article for event in route_a_events),
            *(event.representative.candidate.article for event in route_b_events),
        )
    )
    ranked = NewsRanker().rank(
        [
            *(RankedNewsItem.from_direct_event(event) for event in route_a_events),
            *(RankedNewsItem.from_external_event(event) for event in route_b_events),
        ]
    )
    unsent: list[RankedNewsItem] = []
    already_sent = 0
    same_run_duplicates = 0
    seen_run_fingerprints: set[tuple[str, str]] = set()
    for item in ranked:
        identity = _fingerprint(item)
        if identity in seen_run_fingerprints:
            same_run_duplicates += 1
            continue
        seen_run_fingerprints.add(identity)
        kind, fingerprint = identity
        if profile == "full_shadow":
            # A review run observes the full ranked outcome; it never sends or mutates delivery idempotency.
            unsent.append(item)
        elif store.was_sent(fingerprint, kind=kind):
            already_sent += 1
        else:
            unsent.append(item)
    _log(
        "ranking_complete",
        ranked=len(ranked),
        final_items=len(unsent),
        already_sent=already_sent,
        same_run_duplicates=same_run_duplicates,
    )
    if artifact_journal:
        artifact_journal.update("ranking", journal_ranking_data(unsent))

    status: Literal["success", "inconclusive"] = "success"
    email_items: list[EmailNewsItem] = []
    summary_seconds = 0.0
    render_seconds = 0.0
    email_seconds = 0.0
    delivery_id: str | None = None
    rendered = None
    summary_metrics: dict[str, int] = {
        "summary_calls": 0, "summary_retries": 0, "summary_evidence_retries": 0, "summary_failures": 0,
        "insight_implication_count": 0, "insight_watchpoint_count": 0, "grounding_verifier_calls": 0,
        "grounding_verifier_failures": 0, "unsupported_implications": 0, "watchpoint_rewrites": 0,
    }
    if profile == "smoke" and not unsent:
        status = "inconclusive"
        _log(
            "e2e_inconclusive_no_items",
            freshness_accepted=len(fresh_articles),
            ranked=len(ranked),
            final_items=0,
            email_delivery="skipped",
        )
    else:
        summary_started = monotonic()
        try:
            from openai import OpenAI

            summary_client = OpenAI(timeout=_openai_timeout_seconds())
            summary_model = os.getenv("SUMMARY_MODEL", "gpt-5.6-sol")
            summary_reasoning = os.getenv("SUMMARY_REASONING", "medium")
            grounding_model = os.getenv("GROUNDING_MODEL", "gpt-5.6-luna")
            grounding_reasoning = os.getenv("GROUNDING_REASONING", "medium")
            def summarize_one(item: RankedNewsItem):
                verifier = InsightGroundingVerifier(summary_client, model=grounding_model, reasoning_effort=grounding_reasoning)
                worker = NewsSummarizer(summary_client, model=summary_model, reasoning_effort=summary_reasoning, grounding_verifier=verifier)
                try:
                    output = worker.summarize(item)
                    metrics = worker.metrics.payload() if hasattr(worker, "metrics") else {**summary_metrics, "summary_calls": 1, "insight_watchpoint_count": 1}
                    return item, output, metrics, None
                except Exception as exc:
                    metrics = worker.metrics.payload() if hasattr(worker, "metrics") else {**summary_metrics, "summary_calls": 1, "summary_failures": 1}
                    return item, None, metrics, str(exc)
            summary_concurrency = _positive_int_from_environment("SUMMARY_CONCURRENCY", 4)
            with ThreadPoolExecutor(max_workers=summary_concurrency) as pool:
                summarized = list(pool.map(summarize_one, unsent))
            summary_metrics = {key: sum(metrics.get(key, 0) for _, _, metrics, _ in summarized) for key in summary_metrics}
            email_items = [
                EmailNewsItem(
                    item,
                    output,
                    summary_retry_count=metrics["summary_retries"],
                )
                for item, output, metrics, failure in summarized
                if output is not None and failure is None
            ]
            summary_failures = [
                {"event_id": item.event_id, "error": failure, "metrics": metrics}
                for item, output, metrics, failure in summarized
                if failure is not None
            ]
            if artifact_journal:
                artifact_journal.update("summary", {
                    "metrics": summary_metrics,
                    "successful_items": [{"event_id": value.item.event_id, "summary": value.summary.model_dump()} for value in email_items],
                    "failed_items": summary_failures,
                }, run_status="partial" if summary_failures else "running")
            if summary_failures:
                failed_ids = [value["event_id"] for value in summary_failures]
                if artifact_journal:
                    artifact_journal.fail("one or more final summaries failed", failed_item_ids=failed_ids)
                raise E2EExecutionError("openai_summary", f"final summary failed for event(s): {', '.join(failed_ids)}")
        except Exception as exc:
            if artifact_journal and not isinstance(exc, E2EExecutionError):
                artifact_journal.fail(str(exc))
            if isinstance(exc, E2EExecutionError):
                raise
            raise E2EExecutionError("openai_summary", str(exc)) from exc
        summary_seconds = _seconds(summary_started)
        _log("summary_complete", summary_seconds=summary_seconds, **summary_metrics)

        render_started = monotonic()
        try:
            rendered = HtmlEmailRenderer().render(email_items, report_date=report_date)
            render_seconds = _seconds(render_started)
            if settings is not None:
                email_started = monotonic()
                sender = ResendEmailSender(settings)
                try:
                    delivery_id = sender.send(rendered)
                finally:
                    sender.close()
                email_seconds = _seconds(email_started)
        except Exception as exc:
            stage = "resend_delivery" if settings is not None else "email_render"
            if artifact_journal:
                artifact_journal.fail(f"{stage}: {exc}")
            raise E2EExecutionError(stage, str(exc)) from exc
        _log(
            "email_complete",
            render_seconds=render_seconds,
            email_seconds=email_seconds,
            delivered=delivery_id is not None,
        )

    if delivery_id is not None:
        for item in unsent:
            kind, fingerprint = _fingerprint(item)
            store.mark_sent(fingerprint, kind=kind)

    artifact_json_path: str | None = None
    artifact_html_path: str | None = None
    if profile == "full_shadow":
        if rendered is None:  # pragma: no cover - full shadow always renders, including an empty report
            raise E2EExecutionError("full_shadow_artifact", "full-shadow email report was not rendered")
        artifact_started = monotonic()
        try:
            artifact_json_path, artifact_html_path = write_full_shadow_artifacts(
                artifact_dir=_artifact_dir(store),
                run_time=run_time,
                metrics={
                    "profile": profile,
                    "query_count": len(query_plan.queries),
                    "direct_query_count": len(query_plan.direct_queries),
                    "exposure_query_count": len(query_plan.exposure_queries),
                    "collection_successes": len(collection_result.successes),
                    "collection_failures": len(collection_result.failures),
                    "collection_seconds": collection_seconds,
                    "collected": len(collected),
                    "freshness_seconds": freshness_seconds,
                    "freshness_window_start": freshness.window.start.isoformat(),
                    "freshness_window_end": freshness.window.end.isoformat(),
                    "freshness_mode": freshness.window.mode,
                    "freshness_accepted": len(fresh_articles),
                    "article_deduped": len(article_dedup.articles),
                    "article_duplicates": article_duplicates,
                    "dedup_seconds": dedup_seconds,
                    "routing_seconds": routing_seconds,
                    "route_a_matches": len(route_a_matches),
                    "route_a_events": len(route_a_events),
                    "route_b_candidates": len(candidate_result.candidates),
                    "route_b_accepted": len(accepted),
                    "route_b_rejected": len(candidate_result.rejections) + len(judged) - len(accepted),
                    "judge_seconds": judge_seconds,
                    "judge_calls": len(judged),
                    "summary_seconds": summary_seconds,
                    "summary_calls": len(email_items),
                    "render_seconds": render_seconds,
                    "qualified_route_a_articles": len(route_a_matches),
                    "qualified_route_b_articles": len({result.candidate.article.canonical_url for result in accepted}),
                    "route_a_events_before_dedup": len(route_a_matches),
                    "route_a_events_after_dedup": len(route_a_events),
                    "route_b_events_before_dedup": len({result.candidate.article.canonical_url for result in accepted}),
                    "route_b_events_after_dedup": len(route_b_events),
                    "cross_publication_articles_collapsed": collapsed_events,
                    "duplicate_event_reduction_rate": round(collapsed_events / max(1, event_articles_before), 5),
                    "representative_source_distribution": dict(representative_distribution),
                    "multi_company_external_events": sum(len(event.companies) > 1 for event in route_b_events),
                    **{f"route_a_{key}": value for key, value in route_a_clusterer.metrics.payload().items()},
                    **{f"route_b_{key}": value for key, value in route_b_clusterer.metrics.payload().items()},
                    "deterministic_same_event": route_a_clusterer.metrics.deterministic_same_event + route_b_clusterer.metrics.deterministic_same_event,
                    "deterministic_different_event": route_a_clusterer.metrics.deterministic_different_event + route_b_clusterer.metrics.deterministic_different_event,
                    "ambiguous_pairs": route_a_clusterer.metrics.ambiguous_pairs + route_b_clusterer.metrics.ambiguous_pairs,
                    "luna_event_dedup_calls": route_a_clusterer.metrics.luna_event_dedup_calls + route_b_clusterer.metrics.luna_event_dedup_calls,
                    "luna_event_dedup_failures": route_a_clusterer.metrics.luna_event_dedup_failures + route_b_clusterer.metrics.luna_event_dedup_failures,
                    **summary_metrics,
                    **cascade_metrics,
                },
                delivery_checkpoint_before=(
                    delivery_checkpoint_before.isoformat() if delivery_checkpoint_before is not None else None
                ),
                rendered=rendered,
                email_items=email_items,
                route_a_events=route_a_events,
                route_b_events=route_b_events,
                judged=judged,
                prefilter_rejections=candidate_result.rejections,
                journal=artifact_journal,
            )
        except Exception as exc:
            if artifact_journal:
                artifact_journal.fail(f"full_shadow_artifact: {exc}")
            raise E2EExecutionError("full_shadow_artifact", str(exc)) from exc
        _log(
            "full_shadow_artifacts_complete",
            artifact_seconds=_seconds(artifact_started),
            artifact_json_path=artifact_json_path,
            artifact_html_path=artifact_html_path,
            email_sent=False,
        )

    result = E2EResult(
        status=status,
        profile=profile,
        query_count=len(query_plan.queries),
        direct_query_count=len(query_plan.direct_queries),
        exposure_query_count=len(query_plan.exposure_queries),
        collection_successes=len(collection_result.successes),
        collection_failures=len(collection_result.failures),
        collection_seconds=collection_seconds,
        collected=len(collected),
        freshness_seconds=freshness_seconds,
        freshness_window_start=freshness.window.start.isoformat(),
        freshness_window_end=freshness.window.end.isoformat(),
        freshness_mode=freshness.window.mode,
        freshness_accepted=len(fresh_articles),
        freshness_rejected_too_old=freshness.rejected_too_old,
        freshness_rejected_future=freshness.rejected_future,
        freshness_rejected_missing_timestamp=freshness.rejected_missing_timestamp,
        dedup_seconds=dedup_seconds,
        article_deduped=len(article_dedup.articles),
        article_duplicates=article_duplicates,
        routing_seconds=routing_seconds,
        route_a_matches=len(route_a_matches),
        route_a_events=len(route_a_events),
        route_b_candidates=len(candidate_result.candidates),
        route_b_accepted=len(accepted),
        route_b_rejected=len(candidate_result.rejections) + len(judged) - len(accepted) + len(skipped_for_cap),
        reject_reasons=dict(reasons),
        final_items=len(unsent),
        already_sent=already_sent,
        same_run_duplicates=same_run_duplicates,
        openai_model=openai_model,
        judge_seconds=judge_seconds,
        judge_calls=len(judged),
        cascade_metrics=cascade_metrics,
        summary_seconds=summary_seconds,
        summary_calls=summary_metrics["summary_calls"],
        render_seconds=render_seconds,
        email_seconds=email_seconds,
        total_seconds=_seconds(total_started),
        delivery_id=delivery_id,
        artifact_json_path=artifact_json_path,
        artifact_html_path=artifact_html_path,
        production_delivery_checkpoint_before=(
            delivery_checkpoint_before.isoformat() if delivery_checkpoint_before is not None else None
        ),
    )
    _log("e2e_complete", **result.log_payload())
    return result


def _artifact_dir(store: JsonStateStore) -> Path:
    """Use a mounted persistent directory in Railway; local defaults remain convenient."""
    configured = os.getenv("ARTIFACT_DIR", "").strip()
    return Path(configured) if configured else store.state_dir / "artifacts"
