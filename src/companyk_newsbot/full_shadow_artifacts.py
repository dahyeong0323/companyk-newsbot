"""Review artifacts produced only by the non-delivery full-shadow profile."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
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
        "source_type": article.source_type,
        "url": article.url,
        "canonical_url": article.canonical_url,
        "published_at": article.published_at.isoformat() if article.published_at else None,
        "retrieved_at": article.retrieved_at.isoformat(),
        "description": article.description,
        "text": article.text,
        "language": article.language,
        "origin_metadata": article.origin_metadata,
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
        "event_fingerprint": cluster.semantic_fingerprint or cluster.event_id,
        "canonical_event_label": cluster.event_label or None,
        "grouping_rationale": cluster.grouping_reason or None,
        "representative_selection_reason": cluster.representative_selection_reason or None,
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
        "exact_identity_collapses": [collapse.payload() for collapse in event.exact_identity_collapses],
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
        "summary_retry_count": email_item.summary_retry_count,
        "summary_evidence_retry_count": email_item.summary_retry_count,
        "summary_validation_failure": email_item.summary_validation_failure,
        "summary_failure": email_item.summary_validation_failure is not None,
        "event_id": item.event_id,
        "coverage_count": item.coverage_count,
        "impacted_companies": list(item.impacted_companies),
    }
    if item.direct_match:
        payload["route_a"] = {"matched_aliases": list(item.direct_match.matched_terms)}
    if item.external_match:
        payload["route_b"] = _judged(item.external_match)
    return payload


def journal_collection_data(articles: Iterable[Any], **metrics: Any) -> dict[str, Any]:
    return {"metrics": metrics, "articles": [_article(article) for article in articles]}


def journal_qualification_data(judged: Iterable[JudgedRouteBCandidate], prefilter_rejections: Iterable[RouteBRejection]) -> dict[str, Any]:
    return {
        "judgments": [_judged(value) for value in judged],
        "prefilter_rejections": [
            {"article": _article(value.article), "reason": value.reason, "detail": value.detail}
            for value in prefilter_rejections
        ],
    }


def journal_event_data(route_a_events: Iterable[EventCluster], route_b_events: Iterable[ExternalEventCluster], **metrics: Any) -> dict[str, Any]:
    return {
        "metrics": metrics,
        "route_a_events": [_route_a_event(event) for event in route_a_events],
        "route_b_events": [_route_b_event(event) for event in route_b_events],
    }


def journal_ranking_data(items: Iterable[Any]) -> dict[str, Any]:
    return {
        "items": [
            {
                "event_id": item.event_id,
                "route": item.route,
                "company": item.company,
                "impacted_companies": list(item.impacted_companies),
                "materiality": item.materiality,
                "title": item.article_title,
                "url": item.article_url,
                "coverage_count": item.coverage_count,
            }
            for item in items
        ]
    }


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def preflight_artifact_dir(configured: str) -> Path:
    """Validate an absolute writable artifact target before any paid Full Shadow work."""
    if not configured.strip():
        raise ValueError("full_shadow requires ARTIFACT_DIR on a mounted persistent Railway Volume")
    target = Path(configured.strip()).expanduser()
    if not target.is_absolute():
        raise ValueError("full_shadow ARTIFACT_DIR must be an absolute persistent path")
    target.mkdir(parents=True, exist_ok=True)
    resolved = target.resolve(strict=True)
    probe_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".newsbot-preflight-", suffix=".tmp", dir=resolved, delete=False) as probe:
            probe.write("persistent-artifact-preflight")
            probe.flush()
            os.fsync(probe.fileno())
            probe_name = probe.name
    finally:
        if probe_name is not None:
            Path(probe_name).unlink(missing_ok=True)
    return resolved


class FullShadowArtifactJournal:
    """Atomically persisted run journal that survives failures after expensive stages."""

    def __init__(self, artifact_dir: Path, run_time: datetime) -> None:
        stamp = run_time.strftime("%Y%m%dT%H%M%SZ")
        suffix = ""
        sequence = 0
        while (artifact_dir / f"full_shadow_{stamp}{suffix}.json").exists():
            sequence += 1
            suffix = f"_{sequence:02d}"
        self.json_path = artifact_dir / f"full_shadow_{stamp}{suffix}.json"
        self.html_path = artifact_dir / f"full_shadow_{stamp}{suffix}.html"
        self.payload: dict[str, Any] = {
            "schema_version": "full_shadow_journal_v1",
            "run_at": run_time.isoformat(),
            "run_status": "running",
            "last_completed_stage": "preflight",
            "fatal_error": None,
            "failed_item_ids": [],
            "artifact_updated_at": datetime.now(UTC).isoformat(),
            "stages": {"preflight": {"artifact_dir": str(artifact_dir)}},
        }
        self._persist()

    def update(self, stage: str, data: dict[str, Any], *, run_status: str = "running") -> None:
        self.payload["stages"][stage] = data
        self.payload["last_completed_stage"] = stage
        self.payload["run_status"] = run_status
        self.payload["artifact_updated_at"] = datetime.now(UTC).isoformat()
        self._persist()

    def fail(self, error: str, *, failed_item_ids: Iterable[str] = ()) -> None:
        self.payload["run_status"] = "failed"
        self.payload["fatal_error"] = error
        self.payload["failed_item_ids"] = list(failed_item_ids)
        self.payload["artifact_updated_at"] = datetime.now(UTC).isoformat()
        self._persist()

    def complete(
        self,
        payload: dict[str, Any],
        html: str,
        *,
        run_status: str = "success",
        reason: str | None = None,
    ) -> tuple[str, str]:
        payload["run_status"] = run_status
        payload["last_completed_stage"] = "complete"
        payload["fatal_error"] = reason
        payload["failed_item_ids"] = []
        payload["artifact_updated_at"] = datetime.now(UTC).isoformat()
        payload["debug"]["journal_stages"] = self.payload["stages"]
        _atomic_write_text(self.json_path, json.dumps(payload, ensure_ascii=False, indent=2))
        _atomic_write_text(self.html_path, html)
        self.payload = payload
        return str(self.json_path), str(self.html_path)

    def _persist(self) -> None:
        _atomic_write_text(self.json_path, json.dumps(self.payload, ensure_ascii=False, indent=2))


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
    shadow_delivery_id: str | None = None,
    journal: FullShadowArtifactJournal | None = None,
    debug_extra: dict[str, Any] | None = None,
    run_status: str = "success",
    status_reason: str | None = None,
) -> tuple[str, str]:
    """Persist review data separately from the recipient-facing rendered email."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    journal = journal or FullShadowArtifactJournal(artifact_dir, run_time)
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
                "delivered": False,
                "resend_called": shadow_delivery_id is not None,
                "production_email_sent": False,
                "shadow_test_email_sent": shadow_delivery_id is not None,
                "shadow_test_delivery_id": shadow_delivery_id,
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
            **(debug_extra or {}),
        },
    }
    return journal.complete(payload, rendered.html, run_status=run_status, reason=status_reason)
