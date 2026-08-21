"""Real smoke and non-delivery full-shadow execution for the newsbot."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
import asyncio
import base64
import gzip
import hashlib
import json
import os
from time import monotonic
from typing import Literal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from companyk_newsbot.collectors.google_news_rss import GoogleNewsRSSCollector, normalized_query
from companyk_newsbot.collection_coverage import DEFAULT_RSS_MIN_SUCCESS_RATIO, DEFAULT_ZERO_NEWS_MIN_SUCCESS_RATIO, assess_collection_coverage, assess_zero_news_health
from companyk_newsbot.config import KeywordMapConfig
from companyk_newsbot.portfolio_registry import PortfolioRegistry, build_direct_query_plan
from companyk_newsbot.route_a_only import process_route_a_articles
from companyk_newsbot.semantic_identity import GPT54MiniIdentityProvider
from companyk_newsbot.semantic_grouping import GPT54MiniGroupingProvider
from companyk_newsbot.judges.direct_event import DirectEventGrounder, DirectEventJudge
from companyk_newsbot.enrichment import PublisherArticleEnricher
from companyk_newsbot.dedup import ArticleDeduplicator, LunaEventPairResolver, RepresentativeArticleSelector, RouteAEventClusterer, RouteBEventClusterer
from companyk_newsbot.email import EmailDeliverySettings, EmailNewsItem, HtmlEmailRenderer, RenderedEmail, email_delivery_stage, email_sender_from_settings, email_settings_from_environment
from companyk_newsbot.freshness import FreshnessWindow, delivery_window, filter_articles, full_shadow_window, rss_freshness_hint, smoke_window
from companyk_newsbot.full_shadow_artifacts import FullShadowArtifactJournal, journal_collection_data, journal_event_data, journal_qualification_data, journal_ranking_data, preflight_artifact_dir, write_full_shadow_artifacts
from companyk_newsbot.judges import InsightGroundingVerifier, NewsSummarizer, RouteBCascadeJudge, RouteBCausalMaterialityJudge
from companyk_newsbot.judges.route_b_legacy import RouteBCascadeJudge as LegacyRouteBCascadeJudge
from companyk_newsbot.judges.summary import NewsSummarizer as EditorialPayloadBuilder
from companyk_newsbot.ranking import NewsRanker, RankedNewsItem
from companyk_newsbot.rules import ExposureRegistry, RouteADetector, RouteBCandidateGenerator
from companyk_newsbot.runtime_progress import RuntimeProgress
from companyk_newsbot.state import JsonStateStore


TEST_RECIPIENTS = ("jeremy.cheon@pm.me",)
PRODUCTION_RECIPIENTS = ("jeremy.cheon@pm.me", "taejin3789@naver.com")
KST = ZoneInfo("Asia/Seoul")
DEFAULT_SMOKE_DIRECT_QUERY_CAP = 8
DEFAULT_SMOKE_EXPOSURE_QUERY_CAP = 8
SMOKE_MAX_JUDGE_CALLS = 25
DEFAULT_OPENAI_TIMEOUT_SECONDS = 60.0
DEFAULT_SMOKE_LOOKBACK_DAYS = 7
DEFAULT_FIRST_RUN_HOURS = 30
DEFAULT_OVERLAP_HOURS = 2
DEFAULT_RSS_MAX_LOOKBACK_DAYS = 7
ExecutionProfile = Literal["smoke", "full_shadow", "production"]


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


def _rss_min_success_ratio() -> float:
    return _success_ratio_from_environment("RSS_MIN_SUCCESS_RATIO", DEFAULT_RSS_MIN_SUCCESS_RATIO)


def _zero_news_min_success_ratio() -> float:
    return _success_ratio_from_environment("ZERO_NEWS_MIN_SUCCESS_RATIO", DEFAULT_ZERO_NEWS_MIN_SUCCESS_RATIO)


def _success_ratio_from_environment(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise E2EExecutionError("configuration", f"{name} must be between 0 and 1") from exc
    if not 0 <= value <= 1:
        raise E2EExecutionError("configuration", f"{name} must be between 0 and 1")
    return value


def _cost_first_enabled() -> bool:
    return os.getenv("NEWSBOT_COST_FIRST_PIPELINE", "true").strip().casefold() in {"1", "true", "yes", "on"}


def _route_b_enabled() -> bool:
    return os.getenv("ROUTE_B_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"}


def _route_a_event_resolver_enabled() -> bool:
    return os.getenv("ROUTE_A_EVENT_RESOLVER_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"}


def _require_non_sol_model(stage: str, model: str) -> None:
    if _cost_first_enabled() and "sol" in model.casefold():
        raise E2EExecutionError("configuration", f"cost-first {stage} must not use Sol")


def _usage_from_editorial_traces(traces: list[dict[str, object]]) -> dict[str, int]:
    totals = Counter()
    grounding_events = {"grounding_verification", "watchpoint_grounding", "core_grounding_verification"}
    for trace in traces:
        usage = trace.get("token_usage")
        if not isinstance(usage, dict):
            continue
        prefix = "grounding" if trace.get("event") in grounding_events else "summary"
        for source, target in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")):
            value = usage.get(source)
            if isinstance(value, int):
                totals[f"{prefix}_{target}"] += value
        input_details = usage.get("input_tokens_details")
        if isinstance(input_details, dict) and isinstance(input_details.get("cached_tokens"), int):
            totals[f"{prefix}_cached_input_tokens"] += input_details["cached_tokens"]
        output_details = usage.get("output_tokens_details")
        if isinstance(output_details, dict) and isinstance(output_details.get("reasoning_tokens"), int):
            totals[f"{prefix}_reasoning_tokens"] += output_details["reasoning_tokens"]
    return dict(totals)


def _estimated_stage_cost(metrics: dict[str, object], metric_prefix: str, price_prefix: str) -> float | None:
    """Estimate one stage only when versioned per-million-token prices are configured."""
    price_names = {
        "input": f"{price_prefix}_INPUT_USD_PER_MILLION",
        "cached": f"{price_prefix}_CACHED_INPUT_USD_PER_MILLION",
        "output": f"{price_prefix}_OUTPUT_USD_PER_MILLION",
    }
    raw = {key: os.getenv(name, "").strip() for key, name in price_names.items()}
    if not any(raw.values()):
        return None
    if not all(raw.values()) or not os.getenv("OPENAI_PRICING_VERSION", "").strip():
        raise E2EExecutionError(
            "configuration",
            f"{price_prefix} pricing requires input, cached-input, output, and OPENAI_PRICING_VERSION",
        )
    try:
        prices = {key: float(value) for key, value in raw.items()}
    except ValueError as exc:
        raise E2EExecutionError("configuration", f"invalid {price_prefix} pricing value") from exc
    if any(value < 0 for value in prices.values()):
        raise E2EExecutionError("configuration", f"{price_prefix} pricing values must be non-negative")
    input_tokens = int(metrics.get(f"{metric_prefix}_input_tokens", 0) or 0)
    cached_tokens = int(metrics.get(f"{metric_prefix}_cached_input_tokens", 0) or 0)
    output_tokens = int(metrics.get(f"{metric_prefix}_output_tokens", 0) or 0)
    uncached_tokens = max(0, input_tokens - cached_tokens)
    return round((uncached_tokens * prices["input"] + cached_tokens * prices["cached"] + output_tokens * prices["output"]) / 1_000_000, 8)


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
    elif profile in {"full_shadow", "production"}:
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


def _assert_test_recipient(settings: EmailDeliverySettings) -> None:
    if tuple(recipient.casefold() for recipient in settings.recipients) != TEST_RECIPIENTS:
        raise E2EExecutionError("safety_check", f"test delivery may send only to {TEST_RECIPIENTS[0]}")


def _assert_production_recipient(settings: EmailDeliverySettings) -> None:
    """Keep the configured recipient boundary explicit for the frozen v1 rollout."""
    if tuple(recipient.casefold() for recipient in settings.recipients) != PRODUCTION_RECIPIENTS:
        raise E2EExecutionError("safety_check", "v1 production recipients must match the configured rollout list")


def _openai_timeout_seconds() -> float:
    raw = os.getenv("OPENAI_TIMEOUT_SECONDS", str(DEFAULT_OPENAI_TIMEOUT_SECONDS)).strip()
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise E2EExecutionError("configuration", "OPENAI_TIMEOUT_SECONDS must be positive") from exc
    if timeout <= 0:
        raise E2EExecutionError("configuration", "OPENAI_TIMEOUT_SECONDS must be positive")
    return timeout


def _rss_max_lookback_days() -> int:
    return _positive_int_from_environment("RSS_MAX_LOOKBACK_DAYS", DEFAULT_RSS_MAX_LOOKBACK_DAYS)


def _freshness_window_and_hint(
    *,
    profile: ExecutionProfile,
    now: datetime,
    last_successful_delivery_run: datetime | None,
) -> tuple[FreshnessWindow, str]:
    if profile == "smoke":
        smoke_days = _positive_int_from_environment("E2E_SMOKE_LOOKBACK_DAYS", DEFAULT_SMOKE_LOOKBACK_DAYS)
        return smoke_window(now=now, lookback_days=smoke_days), f"when:{smoke_days}d"

    explicit_shadow_hours = os.getenv("FULL_SHADOW_LOOKBACK_HOURS", "").strip()
    if profile == "full_shadow" and explicit_shadow_hours:
        lookback_hours = _positive_int_from_environment("FULL_SHADOW_LOOKBACK_HOURS", DEFAULT_FIRST_RUN_HOURS)
        window = full_shadow_window(now=now, lookback_hours=lookback_hours)
    else:
        first_run_hours = _positive_int_from_environment("FRESHNESS_FIRST_RUN_HOURS", DEFAULT_FIRST_RUN_HOURS)
        overlap_hours = _positive_int_from_environment("FRESHNESS_OVERLAP_HOURS", DEFAULT_OVERLAP_HOURS)
        window = delivery_window(
            now=now,
            last_successful_delivery_run=last_successful_delivery_run,
            overlap_hours=overlap_hours,
            first_run_hours=first_run_hours,
        )
    return window, rss_freshness_hint(window, maximum_days=_rss_max_lookback_days())


def _fingerprint(item: RankedNewsItem) -> tuple[str, str]:
    kind = "event" if item.route == "direct" else "article"
    if item.route == "direct" and item.direct_event is not None:
        semantic_fingerprint = getattr(item.direct_event, "semantic_fingerprint", "")
        if semantic_fingerprint:
            return kind, semantic_fingerprint
        anchors = item.direct_event.anchors.payload()
        identity = {
            "route": item.route,
            "companies": sorted(item.impacted_companies or (item.company,)),
            "actions": anchors["primary_action_terms"],
            "milestones": anchors["milestone_terms"],
            "counterparties": anchors["counterparties"],
            "dates": anchors["explicit_date_tokens"],
            "amounts": anchors["amount_tokens"],
            "percentages": anchors["percentage_tokens"],
            "families": anchors["event_families"],
        }
        # Only use a cross-run identity when it has an action plus at least one
        # concrete discriminator.  Otherwise separate same-company events can
        # be incorrectly collapsed merely because their headlines are alike.
        discriminators = (
            identity["milestones"], identity["counterparties"], identity["dates"],
            identity["amounts"], identity["percentages"], identity["families"],
        )
        if identity["actions"] and any(discriminators):
            canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            return kind, hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    value = "|".join((item.route, item.company, item.article_url, item.article_title))
    return kind, hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mark_delivery_batch(store: JsonStateStore, items: list[RankedNewsItem]) -> None:
    fingerprints_by_kind: dict[str, list[str]] = {"article": [], "event": []}
    for item in items:
        kind, fingerprint = _fingerprint(item)
        fingerprints_by_kind[kind].append(fingerprint)
    for kind, fingerprints in fingerprints_by_kind.items():
        if fingerprints:
            store.mark_sent_many(fingerprints, kind=kind)


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True), flush=True)


def _editorial_replay_bundle(run_id: str, items: list[RankedNewsItem]) -> dict[str, object]:
    """Freeze the exact post-ranking editor input before any editorial API call."""
    events: list[dict[str, object]] = []
    for rank, item in enumerate(items, start=1):
        # Keep the forensic payload available even when tests substitute the
        # runtime editor with a lightweight fake.
        payload, valid_ids = EditorialPayloadBuilder._payload(item)
        value = json.loads(payload)
        events.append({
            "rank": rank, "event_id": item.event_id, "impacted_companies": list(item.impacted_companies),
            "event_family": value["event_family_context"], "event_anchors": value["canonical_event_anchors"],
            "impact_links": value.get("approved_impact_links", []), "representative_article": value["representative_article"],
            "corroborating_articles": value["corroborating_articles"], "exact_editor_input": value,
            "exact_grounding_evidence_article_ids": sorted(valid_ids),
        })
    return {
        "schema_version": "editorial_replay_bundle_v1", "run_id": run_id,
        "git_commit": os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GITHUB_SHA") or "unknown",
        "events": events,
    }


def _emit_replay_bundle(bundle: dict[str, object], *, chunk_size: int = 6000) -> None:
    compressed = gzip.compress(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    encoded = base64.b64encode(compressed).decode("ascii")
    digest = hashlib.sha256(compressed).hexdigest()
    chunks = [encoded[index:index + chunk_size] for index in range(0, len(encoded), chunk_size)] or [""]
    _log("shadow_replay_begin", run_id=bundle["run_id"], chunks=len(chunks), sha256=digest)
    for seq, chunk in enumerate(chunks, start=1):
        _log("shadow_replay_chunk", run_id=bundle["run_id"], seq=seq, data=chunk)
    _log("shadow_replay_end", run_id=bundle["run_id"], sha256=digest)


def _emit_editorial_traces(run_id: str, traces: list[dict[str, object]]) -> None:
    """Telemetry must never collide with the structured-log event discriminator."""
    for trace in traces:
        trace_for_log = dict(trace)
        trace_for_log["trace_event"] = trace_for_log.pop("event", None)
        _log("editorial_trace", run_id=run_id, **trace_for_log)


def _run_route_a_only_e2e(
    registry: PortfolioRegistry,
    store: JsonStateStore,
    *,
    now: datetime | None,
    profile: ExecutionProfile,
    deliver: bool,
) -> E2EResult:
    """Execute the default direct-news pipeline without loading or calling Route B."""
    total_started = monotonic()
    run_time = (now or datetime.now(UTC)).astimezone(UTC)
    report_date = run_time.astimezone(KST).date()
    settings = email_settings_from_environment() if deliver else None
    if settings is not None:
        if profile == "production":
            _assert_production_recipient(settings)
        else:
            _assert_test_recipient(settings)

    artifact_journal: FullShadowArtifactJournal | None = None
    runtime_progress: RuntimeProgress | None = None
    if profile == "full_shadow":
        try:
            artifact_dir = preflight_artifact_dir(os.getenv("ARTIFACT_DIR", ""))
            artifact_journal = FullShadowArtifactJournal(artifact_dir, run_time)
            runtime_progress = RuntimeProgress(artifact_journal.json_path.with_suffix(".runtime.json"))
        except Exception as exc:
            raise E2EExecutionError("artifact_storage", str(exc)) from exc

    full_plan = build_direct_query_plan(registry)
    if full_plan.uncovered_company_ids:
        raise E2EExecutionError(
            "configuration",
            f"portfolio registry has {len(full_plan.uncovered_company_ids)} companies without direct queries",
        )
    if profile == "smoke":
        cap = _positive_int_from_environment("E2E_DIRECT_QUERY_CAP", DEFAULT_SMOKE_DIRECT_QUERY_CAP)
        direct_queries = _deterministic_sample(full_plan.queries, cap, namespace="route_a_registry")
    elif profile in {"full_shadow", "production"}:
        direct_queries = full_plan.queries
    else:  # pragma: no cover
        raise E2EExecutionError("configuration", f"unknown execution profile: {profile}")
    if not direct_queries:
        raise E2EExecutionError("configuration", "RSS collection requires at least one configured direct query")

    delivery_checkpoint_before = store.last_delivery_datetime()
    freshness_window, freshness_hint = _freshness_window_and_hint(
        profile=profile,
        now=run_time,
        last_successful_delivery_run=delivery_checkpoint_before,
    )
    _log(
        "queries_prepared",
        profile=profile,
        direct=len(direct_queries),
        external=0,
        query_count=len(direct_queries),
        registry_companies=len(registry.companies),
        route_b_enabled=False,
    )

    collection_started = monotonic()
    try:
        async def collect_all():
            async with GoogleNewsRSSCollector(freshness_hint=freshness_hint) as collector:
                return await collector.collect_many(direct_queries)

        collection_result = asyncio.run(collect_all())
    except Exception as exc:
        if artifact_journal:
            artifact_journal.fail(f"collection: {exc}")
        raise E2EExecutionError("collection", str(exc)) from exc
    collection_seconds = _seconds(collection_started)
    collected = list(collection_result.articles)
    rss_metrics = {**collection_result.metrics()}
    try:
        coverage = assess_collection_coverage(collection_result, threshold=_rss_min_success_ratio())
        if profile == "production" and not collected:
            coverage = assess_zero_news_health(collection_result, threshold=_zero_news_min_success_ratio())
    except ValueError as exc:
        if artifact_journal:
            artifact_journal.fail(f"collection_coverage: {exc}")
        raise E2EExecutionError("collection_coverage", str(exc)) from exc
    rss_metrics.update(coverage.metrics())
    skipped_failures = 0
    for failure in collection_result.failures:
        if failure.status == "skipped_systemic_failure":
            skipped_failures += 1
            continue
        _log("collection_query_failed", query=failure.query, status=failure.status, reason=failure.error or failure.status)
    if skipped_failures:
        _log("collection_queries_skipped", status="skipped_systemic_failure", count=skipped_failures)
    _log("collection_coverage_assessed", **rss_metrics)
    if artifact_journal:
        artifact_journal.update("collection", journal_collection_data(
            collected,
            query_count=len(direct_queries),
            collection_successes=len(collection_result.successes),
            collection_failures=len(collection_result.failures),
            collected=len(collected),
            freshness_accepted=None,
        ))
        artifact_journal.update("collection_coverage", {
            **rss_metrics,
            "reason": coverage.reason,
        }, run_status="running" if coverage.sufficient else "inconclusive")

    if not coverage.sufficient:
        reason = coverage.reason or "collection_coverage_below_threshold"
        zero_metrics: dict[str, object] = {
            **rss_metrics,
            "inconclusive_reason": reason,
            "route_b_enabled": False,
            "route_b_calls": 0,
            "article_level_ai_calls": 0,
            "production_sol_calls": 0,
            "direct_assessment_calls": 0,
            "direct_grounding_calls": 0,
            "total_api_estimated_cost_usd": 0.0,
        }
        artifact_json_path: str | None = None
        artifact_html_path: str | None = None
        if profile == "full_shadow":
            diagnostic = RenderedEmail(
                subject=f"[SHADOW][수집 실패] 컴퍼니케이 데일리 | {report_date.isoformat()}",
                html=(
                    "<!doctype html><html lang=\"ko\"><body>"
                    "<h1>뉴스 수집 실패</h1>"
                    f"<p>RSS 수집 성공률이 {coverage.success_ratio:.1%}로 운영 기준 "
                    f"{coverage.threshold:.1%}에 미달했습니다.</p>"
                    "<p>이 문서는 정상 뉴스 브리핑이 아니며 이메일로 발송되지 않았습니다.</p>"
                    "</body></html>"
                ),
            )
            try:
                artifact_json_path, artifact_html_path = write_full_shadow_artifacts(
                    artifact_dir=_artifact_dir(store),
                    run_time=run_time,
                    metrics={
                        "profile": profile,
                        "registry_company_count": len(registry.companies),
                        "query_count": len(direct_queries),
                        "direct_query_count": len(direct_queries),
                        "exposure_query_count": 0,
                        "collection_successes": len(collection_result.successes),
                        "collection_failures": len(collection_result.failures),
                        "collected": len(collected),
                        "final_email_items": 0,
                        **zero_metrics,
                    },
                    delivery_checkpoint_before=(delivery_checkpoint_before.isoformat() if delivery_checkpoint_before else None),
                    rendered=diagnostic,
                    email_items=[],
                    route_a_events=[],
                    route_b_events=[],
                    judged=[],
                    prefilter_rejections=[],
                    shadow_delivery_id=None,
                    journal=artifact_journal,
                    debug_extra={
                        "collection_failure_breakdown": [
                            {
                                "query": failure.query,
                                "status": failure.status,
                                "attempts": failure.attempts,
                                "retry_attempts": failure.retry_attempts,
                                "retry_after_used": failure.retry_after_used,
                                "http_status": failure.http_status,
                            }
                            for failure in collection_result.failures
                        ],
                    },
                    run_status="inconclusive",
                    status_reason=reason,
                )
            except Exception as exc:
                if artifact_journal:
                    artifact_journal.fail(f"full_shadow_artifact: {exc}")
                raise E2EExecutionError("full_shadow_artifact", str(exc)) from exc
        result = E2EResult(
            status="inconclusive",
            profile=profile,
            query_count=len(direct_queries),
            direct_query_count=len(direct_queries),
            exposure_query_count=0,
            collection_successes=len(collection_result.successes),
            collection_failures=len(collection_result.failures),
            collection_seconds=collection_seconds,
            collected=len(collected),
            freshness_seconds=0.0,
            freshness_window_start=freshness_window.start.isoformat(),
            freshness_window_end=freshness_window.end.isoformat(),
            freshness_mode=freshness_window.mode,
            freshness_accepted=0,
            freshness_rejected_too_old=0,
            freshness_rejected_future=0,
            freshness_rejected_missing_timestamp=0,
            dedup_seconds=0.0,
            article_deduped=0,
            article_duplicates=0,
            routing_seconds=0.0,
            route_a_matches=0,
            route_a_events=0,
            route_b_candidates=0,
            route_b_accepted=0,
            route_b_rejected=0,
            reject_reasons={reason: len(collection_result.failures)},
            final_items=0,
            already_sent=0,
            same_run_duplicates=0,
            openai_model=os.getenv("DIRECT_EVENT_MODEL", "gpt-5.6-luna"),
            judge_seconds=0.0,
            judge_calls=0,
            cascade_metrics=zero_metrics,
            summary_seconds=0.0,
            summary_calls=0,
            render_seconds=0.0,
            email_seconds=0.0,
            total_seconds=_seconds(total_started),
            delivery_id=None,
            artifact_json_path=artifact_json_path,
            artifact_html_path=artifact_html_path,
            production_delivery_checkpoint_before=(delivery_checkpoint_before.isoformat() if delivery_checkpoint_before else None),
        )
        if runtime_progress: runtime_progress.finish("inconclusive")
        _log("e2e_complete", **result.log_payload())
        return result

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
            query_count=len(direct_queries),
            collection_successes=len(collection_result.successes),
            collection_failures=len(collection_result.failures),
            collected=len(collected),
            freshness_accepted=len(fresh_articles),
        ))

    dedup_started = monotonic()
    try:
        article_dedup = ArticleDeduplicator().deduplicate(fresh_articles)
    except Exception as exc:
        if artifact_journal:
            artifact_journal.fail(f"article_dedup: {exc}")
        raise E2EExecutionError("article_dedup", str(exc)) from exc
    dedup_seconds = _seconds(dedup_started)
    article_duplicates = sum(len(group.duplicates) for group in article_dedup.duplicate_groups)

    enrichment_started = monotonic()
    try:
        enrichment = asyncio.run(
            PublisherArticleEnricher.from_environment().enrich_all(list(article_dedup.articles), registry)
        )
    except Exception as exc:
        if artifact_journal:
            artifact_journal.fail(f"article_enrichment: {exc}")
        raise E2EExecutionError("article_enrichment", str(exc)) from exc
    enrichment_seconds = _seconds(enrichment_started)
    enrichment_metrics = enrichment.metrics.payload()
    if artifact_journal:
        # Preserve complete, model-ready Article payloads. This allows an
        # interrupted Full Shadow to replay downstream semantics without RSS
        # collection or another publisher-enrichment pass.
        artifact_journal.update("enrichment", journal_collection_data(
            enrichment.articles, **{**enrichment_metrics, "enrichment_seconds": enrichment_seconds}
        ))

    judge = DirectEventJudge.from_environment()
    grounder = DirectEventGrounder.from_environment()
    model_first_enabled = bool(os.getenv("OPENAI_API_KEY", "").strip())
    identity_provider = GPT54MiniIdentityProvider.from_environment() if model_first_enabled else None
    grouping_provider = GPT54MiniGroupingProvider.from_environment() if model_first_enabled else None
    event_resolver = LunaEventPairResolver.from_environment() if not model_first_enabled and _route_a_event_resolver_enabled() else None
    processing_started = monotonic()
    try:
        processed = process_route_a_articles(
            list(enrichment.articles),
            registry,
            judge=judge,
            grounder=grounder,
            identity_provider=identity_provider,
            grouping_provider=grouping_provider,
            event_resolver=event_resolver,
            forensic_progress=runtime_progress.event if runtime_progress else None,
        )
    except Exception as exc:
        if runtime_progress: runtime_progress.finish("failed")
        if artifact_journal:
            artifact_journal.fail(f"route_a_processing: {exc}")
        raise E2EExecutionError("route_a_processing", str(exc)) from exc
    processing_seconds = _seconds(processing_started)
    assessment_metrics = judge.metrics.payload("direct_assessment")
    grounding_metrics = grounder.metrics.payload("direct_grounding")
    assessment_cost = _estimated_stage_cost(assessment_metrics, "direct_assessment", "DIRECT_EVENT")
    grounding_cost = _estimated_stage_cost(grounding_metrics, "direct_grounding", "DIRECT_GROUNDING")
    cascade_metrics: dict[str, object] = {
        **rss_metrics,
        **enrichment_metrics,
        "enrichment_seconds": enrichment_seconds,
        **assessment_metrics,
        **grounding_metrics,
        **(identity_provider.metrics_payload() if identity_provider else {"identity_requests": 0, "identity_failures": 0}),
        **(grouping_provider.metrics_payload() if grouping_provider else {"grouping_requests": 0, "grouping_failures": 0}),
        "route_b_enabled": False,
        "route_b_calls": 0,
        "article_level_ai_calls": 0,
        "production_sol_calls": 0,
        "direct_assessment_estimated_cost_usd": assessment_cost,
        "direct_grounding_estimated_cost_usd": grounding_cost,
        "total_api_estimated_cost_usd": (
            round((assessment_cost or 0.0) + (grounding_cost or 0.0), 8)
            if assessment_cost is not None and grounding_cost is not None else None
        ),
        "direct_deliver_high": processed.deliver_high,
        "direct_deliver_medium": processed.deliver_medium,
        "direct_ignore": processed.ignore_count,
        "direct_event_model_failure_events": processed.model_failure_events,
        **(processed.model_metrics or {}),
    }
    if processed.systemic_model_failure:
        if runtime_progress: runtime_progress.finish("inconclusive")
        result = E2EResult(status="inconclusive", profile=profile, query_count=len(direct_queries),
            direct_query_count=len(direct_queries), exposure_query_count=0,
            collection_successes=len(collection_result.successes), collection_failures=len(collection_result.failures),
            collection_seconds=collection_seconds, collected=len(collected), freshness_seconds=freshness_seconds,
            freshness_window_start=freshness.window.start.isoformat(), freshness_window_end=freshness.window.end.isoformat(),
            freshness_mode=freshness.window.mode, freshness_accepted=len(fresh_articles),
            freshness_rejected_too_old=freshness.rejected_too_old, freshness_rejected_future=freshness.rejected_future,
            freshness_rejected_missing_timestamp=freshness.rejected_missing_timestamp, dedup_seconds=dedup_seconds,
            article_deduped=len(article_dedup.articles), article_duplicates=article_duplicates,
            routing_seconds=processing_seconds, route_a_matches=len(processed.matches), route_a_events=len(processed.events),
            route_b_candidates=0, route_b_accepted=0, route_b_rejected=0,
            reject_reasons={"direct_event_model_failure_rate": processed.model_failure_events}, final_items=0,
            already_sent=0, same_run_duplicates=0, openai_model=judge.model, judge_seconds=processing_seconds,
            judge_calls=judge.metrics.calls, cascade_metrics={**cascade_metrics, "inconclusive_reason": "direct_event_model_failure_rate"},
            summary_seconds=0.0, summary_calls=0, render_seconds=0.0, email_seconds=0.0,
            total_seconds=_seconds(total_started), delivery_id=None, artifact_json_path=None, artifact_html_path=None,
            production_delivery_checkpoint_before=(delivery_checkpoint_before.isoformat() if delivery_checkpoint_before else None))
        _log("e2e_complete", **result.log_payload())
        return result
    reject_reasons = {"direct_event:IGNORE": processed.ignore_count} if processed.ignore_count else {}
    _log(
        "route_a_processing_complete",
        processing_seconds=processing_seconds,
        article_deduped=len(article_dedup.articles),
        article_duplicates=article_duplicates,
        route_a_matches=len(processed.matches),
        route_a_events=len(processed.events),
        final_items=len(processed.email_items),
        **cascade_metrics,
    )

    ranked = list(processed.ranked_items)
    email_items = list(processed.email_items)
    company_stage_counts = {
        company.company_id: {"company_id": company.company_id, "company": company.display_name,
            "rss_collected": 0, "fresh": 0, "mechanical_dedup_retained": 0,
            "identity_related": 0, "identity_not_related": 0, "identity_uncertain": 0,
            "canonical_events": 0, "deliver": 0, "ignore": 0}
        for company in registry.companies
    }
    def count_company_stage(stage: str, articles):
        for article in articles:
            ids = article.origin_metadata.get("candidate_company_ids", [])
            if isinstance(ids, str): ids = [ids]
            for company_id in ids:
                row = company_stage_counts.get(company_id)
                if row is not None: row[stage] += 1
    count_company_stage("rss_collected", collected)
    count_company_stage("fresh", fresh_articles)
    count_company_stage("mechanical_dedup_retained", article_dedup.articles)
    for row in (processed.model_metrics or {}).get("company_stage_counts", []):
        if not isinstance(row, dict): continue
        target = company_stage_counts.get(row.get("company_id"))
        if target is not None:
            for key in ("identity_related", "identity_not_related", "identity_uncertain", "canonical_events", "deliver", "ignore"):
                target[key] = int(row.get(key, 0) or 0)
    already_sent = 0
    same_run_duplicates = 0
    if profile != "full_shadow":
        keep_indexes: list[int] = []
        seen_run_fingerprints: set[tuple[str, str]] = set()
        for index, item in enumerate(ranked):
            identity = _fingerprint(item)
            if identity in seen_run_fingerprints:
                same_run_duplicates += 1
                continue
            seen_run_fingerprints.add(identity)
            kind, fingerprint = identity
            if store.was_sent(fingerprint, kind=kind):
                already_sent += 1
            else:
                keep_indexes.append(index)
        ranked = [ranked[index] for index in keep_indexes]
        email_items = [email_items[index] for index in keep_indexes]

    render_started = monotonic()
    try:
        rendered = HtmlEmailRenderer().render(email_items, report_date=report_date, route_b_enabled=False)
        if profile == "full_shadow":
            rendered = RenderedEmail(
                subject=f"[SHADOW] 컴퍼니케이 데일리 | {report_date.isoformat()} | 주요 뉴스 {len(email_items)}건",
                html=rendered.html,
            )
    except Exception as exc:
        if artifact_journal:
            artifact_journal.fail(f"email_render: {exc}")
        raise E2EExecutionError("email_render", str(exc)) from exc
    render_seconds = _seconds(render_started)
    delivery_id: str | None = None
    email_seconds = 0.0
    if settings is not None:
        email_started = monotonic()
        sender = email_sender_from_settings(settings)
        try:
            delivery_id = sender.send(rendered)
        except Exception as exc:
            stage = email_delivery_stage(settings)
            if artifact_journal:
                artifact_journal.fail(f"{stage}: {exc}")
            raise E2EExecutionError(stage, str(exc)) from exc
        finally:
            sender.close()
        email_seconds = _seconds(email_started)

    if delivery_id is not None and profile != "full_shadow":
        _mark_delivery_batch(store, ranked)

    artifact_json_path: str | None = None
    artifact_html_path: str | None = None
    if profile == "full_shadow":
        try:
            artifact_json_path, artifact_html_path = write_full_shadow_artifacts(
                artifact_dir=_artifact_dir(store),
                run_time=run_time,
                metrics={
                    "profile": profile,
                    "registry_company_count": len(registry.companies),
                    "query_count": len(direct_queries),
                    "direct_query_count": len(direct_queries),
                    "exposure_query_count": 0,
                    "collection_successes": len(collection_result.successes),
                    "collection_failures": len(collection_result.failures),
                    "collected": len(collected),
                    "freshness_accepted": len(fresh_articles),
                    "article_deduped": len(article_dedup.articles),
                    "article_duplicates": article_duplicates,
                    "dedup_seconds": dedup_seconds,
                    "enrichment_seconds": enrichment_seconds,
                    "route_a_matches": len(processed.matches),
                    "route_a_events": len(processed.events),
                    "route_b_candidates": 0,
                    "route_b_accepted": 0,
                    "route_b_rejected": 0,
                    "final_email_items": len(email_items),
                    "unsupported_fact_fallbacks": sum(v.fact_summary == "UNSUPPORTED" for v in processed.grounding_verdicts.values()),
                    "unsupported_insights_dropped": sum(v.investor_insight == "UNSUPPORTED" for v in processed.grounding_verdicts.values()),
                    "unsupported_output_delivered": 0,
                    **cascade_metrics,
                },
                delivery_checkpoint_before=(delivery_checkpoint_before.isoformat() if delivery_checkpoint_before else None),
                rendered=rendered,
                email_items=email_items,
                route_a_events=list(processed.events),
                route_b_events=[],
                judged=[],
                prefilter_rejections=[],
                shadow_delivery_id=delivery_id,
                journal=artifact_journal,
                debug_extra={
                    "portfolio_registry": registry.source.model_dump(),
                    "direct_query_coverage": {
                        "query_count": len(full_plan.queries),
                        "attempted_query_count": len(direct_queries),
                        "company_ids_by_normalized_query": full_plan.company_ids_by_query,
                        "uncovered_company_ids": list(full_plan.uncovered_company_ids),
                    },
                    "direct_event_assessments": {key: value.model_dump() for key, value in processed.assessments.items()},
                    "direct_grounding_verdicts": {key: value.model_dump() for key, value in processed.grounding_verdicts.items()},
                    "identity_decisions": (processed.model_metrics or {}).get("identity_decisions", []),
                    "company_stage_counts": list(company_stage_counts.values()),
                    "enrichment_audit": [
                        {
                            "article_url": article.url,
                            "canonical_url": article.canonical_url,
                            "origin_queries": article.origin_metadata.get("origin_queries", []),
                            "candidate_company_ids": article.origin_metadata.get("candidate_company_ids", []),
                            "enrichment_attempted": article.origin_metadata.get("enrichment_attempted", False),
                            "enrichment_status": article.origin_metadata.get("enrichment_status"),
                            "resolved_url": article.origin_metadata.get("resolved_url"),
                            "enrichment_source": article.origin_metadata.get("enrichment_source"),
                            "enriched_char_count": article.origin_metadata.get("enriched_char_count", 0),
                        }
                        for article in enrichment.articles
                    ],
                },
            )
        except Exception as exc:
            if artifact_journal:
                artifact_journal.fail(f"full_shadow_artifact: {exc}")
            raise E2EExecutionError("full_shadow_artifact", str(exc)) from exc

    result = E2EResult(
        status="success",
        profile=profile,
        query_count=len(direct_queries),
        direct_query_count=len(direct_queries),
        exposure_query_count=0,
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
        routing_seconds=processing_seconds,
        route_a_matches=len(processed.matches),
        route_a_events=len(processed.events),
        route_b_candidates=0,
        route_b_accepted=0,
        route_b_rejected=0,
        reject_reasons=reject_reasons,
        final_items=len(email_items),
        already_sent=already_sent,
        same_run_duplicates=same_run_duplicates,
        openai_model=judge.model,
        judge_seconds=processing_seconds,
        judge_calls=judge.metrics.calls,
        cascade_metrics=cascade_metrics,
        summary_seconds=0.0,
        summary_calls=0,
        render_seconds=render_seconds,
        email_seconds=email_seconds,
        total_seconds=_seconds(total_started),
        delivery_id=delivery_id,
        artifact_json_path=artifact_json_path,
        artifact_html_path=artifact_html_path,
        production_delivery_checkpoint_before=(delivery_checkpoint_before.isoformat() if delivery_checkpoint_before else None),
    )
    if runtime_progress: runtime_progress.finish("success")
    _log("e2e_complete", **result.log_payload())
    return result


def run_real_e2e(
    config: KeywordMapConfig | PortfolioRegistry,
    store: JsonStateStore,
    *,
    now: datetime | None = None,
    profile: ExecutionProfile = "smoke",
    deliver: bool = True,
) -> E2EResult:
    """Run real services with profile-specific recipient and delivery safety checks."""
    if profile == "production" and _route_b_enabled():
        raise E2EExecutionError("configuration", "production requires ROUTE_B_ENABLED=false")
    if not _route_b_enabled():
        registry = config if isinstance(config, PortfolioRegistry) else PortfolioRegistry.from_legacy(config)
        return _run_route_a_only_e2e(registry, store, now=now, profile=profile, deliver=deliver)
    if not isinstance(config, KeywordMapConfig):
        raise E2EExecutionError("configuration", "ROUTE_B_ENABLED requires legacy keyword map config")
    total_started = monotonic()
    run_time = (now or datetime.now(UTC)).astimezone(UTC)
    run_id = f"full_shadow_{run_time.strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:12]}"
    report_date = run_time.astimezone(KST).date()
    settings = email_settings_from_environment() if deliver else None
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
    freshness_window, freshness_hint = _freshness_window_and_hint(
        profile=profile,
        now=run_time,
        last_successful_delivery_run=delivery_checkpoint_before,
    )
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
    openai_model = (
        os.getenv("ROUTE_B_NANO_MODEL", "gpt-5.4-nano").strip()
        if _cost_first_enabled()
        else os.getenv("ROUTE_B_PRIMARY_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.6-sol")).strip()
    )
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
        if profile == "full_shadow" or _cost_first_enabled():
            cascade_class = RouteBCascadeJudge if _cost_first_enabled() else LegacyRouteBCascadeJudge
            cascade = cascade_class.from_environment()
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
    event_resolver_metrics = (
        event_resolver.metrics_payload()
        if event_resolver is not None and hasattr(event_resolver, "metrics_payload")
        else {}
    )
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
    replay_bundle: dict[str, object] | None = None
    if profile == "full_shadow":
        replay_bundle = _editorial_replay_bundle(run_id, unsent)
        if artifact_journal:
            artifact_journal.update("editorial_replay_bundle", replay_bundle)
        _emit_replay_bundle(replay_bundle)

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
        "watchpoint_fallbacks": 0, "core_rewrites": 0, "core_fallbacks": 0,
        "summary_input_tokens": 0, "summary_cached_input_tokens": 0, "summary_output_tokens": 0,
        "summary_reasoning_tokens": 0, "grounding_input_tokens": 0, "grounding_cached_input_tokens": 0,
        "grounding_output_tokens": 0, "grounding_reasoning_tokens": 0,
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
            summary_model = os.getenv("SUMMARY_MODEL", "gpt-5.6-luna")
            summary_reasoning = os.getenv("SUMMARY_REASONING", "low")
            grounding_model = os.getenv("GROUNDING_MODEL", "gpt-5.6-luna")
            grounding_reasoning = os.getenv("GROUNDING_REASONING", "low")
            _require_non_sol_model("editorial model", summary_model)
            _require_non_sol_model("grounding model", grounding_model)
            def summarize_one(item: RankedNewsItem):
                verifier = InsightGroundingVerifier(summary_client, model=grounding_model, reasoning_effort=grounding_reasoning)
                worker = NewsSummarizer(summary_client, model=summary_model, reasoning_effort=summary_reasoning, grounding_verifier=verifier)
                try:
                    output = worker.summarize(item)
                    metrics = worker.metrics.payload() if hasattr(worker, "metrics") else {**summary_metrics, "summary_calls": 1, "insight_watchpoint_count": 1}
                    metrics.update(_usage_from_editorial_traces(getattr(worker, "forensic_trace", [])))
                    return item, output, metrics, None, getattr(worker, "forensic_trace", [])
                except Exception as exc:
                    metrics = worker.metrics.payload() if hasattr(worker, "metrics") else {**summary_metrics, "summary_calls": 1, "summary_failures": 1}
                    metrics.update(_usage_from_editorial_traces(getattr(worker, "forensic_trace", [])))
                    return item, None, metrics, str(exc), getattr(worker, "forensic_trace", [])
            summary_concurrency = _positive_int_from_environment("SUMMARY_CONCURRENCY", 4)
            with ThreadPoolExecutor(max_workers=summary_concurrency) as pool:
                summarized = list(pool.map(summarize_one, unsent))
            summary_metrics = {key: sum(metrics.get(key, 0) for _, _, metrics, _, _ in summarized) for key in summary_metrics}
            editorial_traces = [trace for _, _, _, _, traces in summarized for trace in traces]
            _emit_editorial_traces(run_id, editorial_traces)
            email_items = [
                EmailNewsItem(
                    item,
                    output,
                    summary_retry_count=metrics["summary_retries"],
                )
                for item, output, metrics, failure, _ in summarized
                if output is not None and failure is None
            ]
            summary_failures = [
                {"event_id": item.event_id, "error": failure, "metrics": metrics}
                for item, output, metrics, failure, _ in summarized
                if failure is not None
            ]
            if artifact_journal:
                artifact_journal.update("summary", {
                    "metrics": summary_metrics,
                    "run_id": run_id,
                    "editorial_trace": editorial_traces,
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
            if profile == "full_shadow":
                rendered = RenderedEmail(
                    subject=f"[SHADOW] 컴퍼니케이 데일리 | {report_date.isoformat()} | 주요 뉴스 {len(email_items)}건",
                    html=rendered.html,
                )
            render_seconds = _seconds(render_started)
            if settings is not None:
                email_started = monotonic()
                sender = email_sender_from_settings(settings)
                try:
                    delivery_id = sender.send(rendered)
                finally:
                    sender.close()
                email_seconds = _seconds(email_started)
        except Exception as exc:
            stage = email_delivery_stage(settings) if settings is not None else "email_render"
            if artifact_journal:
                artifact_journal.fail(f"{stage}: {exc}")
            raise E2EExecutionError(stage, str(exc)) from exc
        _log(
            "email_complete",
            render_seconds=render_seconds,
            email_seconds=email_seconds,
            delivered=delivery_id is not None,
        )

    if delivery_id is not None and profile != "full_shadow":
        _mark_delivery_batch(store, unsent)

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
                    "final_email_items": len(email_items),
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
                    **event_resolver_metrics,
                    **summary_metrics,
                    **cascade_metrics,
                    "unsupported_output_delivered": 0,
                    "production_sol_calls": 0,
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
                shadow_delivery_id=delivery_id,
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
            production_email_sent=False,
            shadow_test_email_sent=delivery_id is not None,
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
