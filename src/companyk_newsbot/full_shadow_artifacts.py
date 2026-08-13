"""Review artifacts produced only by the non-delivery full-shadow profile."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from companyk_newsbot.dedup import EventCluster, ExternalEventCluster, article_id
from companyk_newsbot.email import EmailNewsItem, RenderedEmail
from companyk_newsbot.judges import JudgedRouteBCandidate
from companyk_newsbot.rules import RouteBRejection


def _article(article: Any) -> dict[str, Any]:
    return {
        "article_id": article_id(article),
        "title": article.title,
        "source": article.source,
        "url": article.canonical_url,
        "published_at": article.published_at.isoformat() if article.published_at else None,
    }


def _event_id(cluster: EventCluster) -> str:
    value = f"{cluster.company}|{cluster.primary.article.canonical_url}|{cluster.primary.article.title}"
    return f"route_a_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def _route_a_event(cluster: EventCluster) -> dict[str, Any]:
    members = (cluster.primary, *cluster.coverage)
    return {
        "event_id": cluster.event_id or _event_id(cluster),
        "route": "Route A",
        "company": cluster.company,
        "matched_aliases": list(cluster.primary.matched_terms),
        "primary_article": _article(cluster.primary.article),
        "member_articles": [
            {**_article(member.article), "representative_score": cluster.representative_scores[article_id(member.article)].total, "score_breakdown": cluster.representative_scores[article_id(member.article)].payload()}
            for member in members
        ],
        "representative_article_id": article_id(cluster.primary.article),
        "coverage_count": cluster.coverage_count,
        "event_anchors": cluster.anchors.payload(),
        "pairwise_dedup_decisions": [decision.payload() for decision in cluster.dedup_decisions],
    }


def _route_b_event(event: ExternalEventCluster) -> dict[str, Any]:
    articles = event.all_articles
    return {
        "event_id": event.event_id,
        "route": "Route B",
        "event_family": event.event_family,
        "source_families": list(event.source_families),
        "impacted_companies": list(event.companies),
        "representative_article_id": article_id(event.representative.candidate.article),
        "member_articles": [
            {**_article(article), "representative_score": event.representative_scores[article_id(article)].total, "score_breakdown": event.representative_scores[article_id(article)].payload()}
            for article in articles
        ],
        "coverage_count": event.coverage_count,
        "event_anchors": event.anchors.payload(),
        "pairwise_dedup_decisions": [decision.payload() for decision in event.dedup_decisions],
        "impact_links": [_judged(value) for value in event.impact_links],
        "aggregate_materiality": event.materiality,
    }


def _judged(value: JudgedRouteBCandidate) -> dict[str, Any]:
    candidate, decision = value.candidate, value.decision
    return {
        "company": candidate.company,
        "article": _article(candidate.article),
        "exposure": {
            "exposure_id": candidate.exposure_id,
            "subject": candidate.exposure_subject,
            "allowed_event_families": list(candidate.allowed_event_families),
        },
        "judge": {
            "model": value.model,
            "prompt_version": value.prompt_version,
            "qualifies": decision.qualifies,
            "event_family": decision.event_family,
            "materiality": decision.materiality,
            "impact_direction": decision.impact_direction,
            "causal_mechanism": decision.causal_mechanism,
            "rejection_reason": decision.rejection_reason,
            "audit": value.audit,
        },
    }


def _rejection_sample(values: Iterable[JudgedRouteBCandidate], limit: int = 20) -> list[dict[str, Any]]:
    """A deterministic round-robin sample so one rejection reason cannot dominate."""
    grouped: dict[str, list[JudgedRouteBCandidate]] = defaultdict(list)
    for value in values:
        if not value.decision.qualifies:
            grouped[value.decision.rejection_reason].append(value)
    for group in grouped.values():
        group.sort(key=lambda value: (value.candidate.company, value.candidate.article.canonical_url))
    sample: list[dict[str, Any]] = []
    offset = 0
    while len(sample) < limit:
        added = False
        for reason in sorted(grouped):
            group = grouped[reason]
            if offset < len(group):
                sample.append(_judged(group[offset]))
                added = True
                if len(sample) == limit:
                    break
        if not added:
            break
        offset += 1
    return sample


def _final_item(position: int, email_item: EmailNewsItem) -> dict[str, Any]:
    item = email_item.item
    payload: dict[str, Any] = {
        "ranking_position": position,
        "route": "Route A" if item.route == "direct" else "Route B",
        "company": item.company,
        "title": item.article_title,
        "url": item.article_url,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "materiality": item.materiality,
        "summary": email_item.summary.summary,
        "why_it_matters": email_item.summary.why_it_matters,
        "insight_one_liner": email_item.summary.insight_one_liner,
        "insight_dimension": email_item.summary.insight_dimension,
        "insight_mode": email_item.summary.insight_mode,
        "insight_confidence": email_item.summary.confidence,
        "evidence_article_ids": email_item.summary.evidence_article_ids,
        "summary_evidence_retry_count": email_item.summary_evidence_retry_count,
        "summary_failure": email_item.summary_failure,
        "event_id": item.event_id,
        "coverage_count": item.coverage_count,
        "impacted_companies": list(item.impacted_companies),
    }
    if item.direct_match:
        payload["route_a"] = {"matched_aliases": list(item.direct_match.matched_terms)}
    if item.external_match:
        payload["route_b"] = _judged(item.external_match)
    return payload


def write_full_shadow_artifacts(
    *,
    artifact_dir: Path,
    run_time: datetime,
    metrics: dict[str, Any],
    delivery_checkpoint_before: str | None,
    rendered: RenderedEmail,
    email_items: list[EmailNewsItem],
    route_a_events: list[EventCluster],
    route_b_events: list[ExternalEventCluster],
    judged: list[JudgedRouteBCandidate],
    prefilter_rejections: Iterable[RouteBRejection],
) -> tuple[str, str]:
    """Persist review data separately from the recipient-facing rendered email."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = run_time.strftime("%Y%m%dT%H%M%SZ")
    json_path = artifact_dir / f"full_shadow_{stamp}.json"
    html_path = artifact_dir / f"full_shadow_{stamp}.html"
    payload = {
        "schema_version": "full_shadow_review_v1",
        "run_at": run_time.isoformat(),
        "user_facing": {
            "email_subject": rendered.subject,
            "final_items": [_final_item(index, item) for index, item in enumerate(email_items, start=1)],
        },
        "debug": {
            "metrics": metrics,
            "safety": {
                "email_sent": False,
                "resend_called": False,
                "production_delivery_checkpoint_before": delivery_checkpoint_before,
                "production_delivery_checkpoint_advanced_by_run": False,
            },
            "route_a_events": [_route_a_event(event) for event in route_a_events],
            "route_b": {
                "events": [_route_b_event(event) for event in route_b_events],
                "judgments": [_judged(value) for value in judged],
                "rejected_sample": _rejection_sample(judged),
                "prefilter_rejections": [
                    {"article": _article(value.article), "reason": value.reason, "detail": value.detail}
                    for value in prefilter_rejections
                ],
            },
            "openai_usage": "not_measured_by_current_sdk_wrapper",
        },
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(rendered.html, encoding="utf-8")
    return str(json_path), str(html_path)
