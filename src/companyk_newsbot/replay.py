"""Classifier-only replay against a frozen full-shadow Route B corpus."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from companyk_newsbot.judges import RouteBCascadeJudge, candidate_id
from companyk_newsbot.models import Article
from companyk_newsbot.rules import RouteBCandidate


class ReplayError(RuntimeError):
    pass


_EXACT_ARTICLE_FIELDS = {
    "source", "source_type", "title", "url", "published_at", "retrieved_at",
    "description", "text", "origin_metadata",
}


def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        records = payload["debug"]["route_b"]["judgments"]
    except (KeyError, TypeError):
        try:
            records = payload["stages"]["qualification"]["judgments"]
        except (KeyError, TypeError) as exc:
            raise ReplayError("baseline artifact has no Route B judgments") from exc
    if not isinstance(records, list) or not records:
        raise ReplayError("baseline artifact has no Route B judgments")
    return records


def _candidate(record: dict[str, Any]) -> RouteBCandidate:
    try:
        article, exposure = record["article"], record["exposure"]
        missing = sorted(_EXACT_ARTICLE_FIELDS - set(article))
        if missing:
            raise ReplayError(
                "exact classifier input is unrecoverable; stored article is missing: " + ", ".join(missing)
            )
        published = article["published_at"]
        retrieved = article["retrieved_at"]
        return RouteBCandidate(
            article=Article(
                source=article["source"],
                source_type=article["source_type"],
                title=article["title"],
                url=article["url"],
                canonical_url=article["url"],
                description=article["description"],
                text=article["text"],
                published_at=datetime.fromisoformat(published) if published else None,
                retrieved_at=datetime.fromisoformat(retrieved) if retrieved else datetime.now(UTC),
                language=article.get("language"),
                origin_metadata=article["origin_metadata"],
            ),
            company=record["company"],
            exposure_id=exposure["exposure_id"],
            exposure_subject=exposure["subject"],
            allowed_event_families=tuple(exposure["allowed_event_families"]),
        )
    except ReplayError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayError("invalid Route B judgment record") from exc


def _old_final(record: dict[str, Any]) -> str:
    judge = record.get("judge", {})
    audit = judge.get("audit", {}) if isinstance(judge, dict) else {}
    value = audit.get("final_decision") if isinstance(audit, dict) else None
    if value in {"ACCEPT", "REJECT"}:
        return value
    return "ACCEPT" if judge.get("qualifies") else "REJECT"


async def run_replay(artifact_path: Path, *, judge: RouteBCascadeJudge | None = None) -> dict[str, Any]:
    """Run only nano and escalated Luna; never collect, dedup, rank, send, or mutate state."""
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"invalid full-shadow baseline artifact: {artifact_path}") from exc
    records = _records(payload)
    candidates = [_candidate(record) for record in records]
    cascade = judge or RouteBCascadeJudge.from_environment()
    results = await cascade.judge_all(candidates)
    by_id = {candidate_id(result.candidate): result for result in results}

    rows: list[dict[str, Any]] = []
    for record, candidate in zip(records, candidates):
        result = by_id[candidate_id(candidate)]
        old = _old_final(record)
        new = "ACCEPT" if result.decision.qualifies else "REJECT"
        old_audit = record.get("judge", {}).get("audit", {})
        rows.append({
            "candidate_id": candidate_id(candidate),
            "company": candidate.company,
            "exposure_id": candidate.exposure_id,
            "article_title": candidate.article.title,
            "article_url": candidate.article.canonical_url,
            "baseline_luna_decision": old_audit.get("luna_decision"),
            "baseline_sol_invoked": bool(old_audit.get("sol_invoked")),
            "baseline_final_decision": old,
            "nano_decision": result.audit.get("nano_decision"),
            "nano_reason_code": result.audit.get("nano_reason_code"),
            "luna_invoked": result.audit.get("luna_invoked"),
            "luna_decision": result.audit.get("luna_decision"),
            "accepted_due_to_classifier_failure": result.audit.get("accepted_due_to_classifier_failure", False),
            "new_final_decision": new,
        })

    positives = [row for row in rows if row["baseline_final_decision"] == "ACCEPT"]
    negatives = [row for row in rows if row["baseline_final_decision"] == "REJECT"]
    lost = [row for row in positives if row["new_final_decision"] == "REJECT"]
    gained = [row for row in negatives if row["new_final_decision"] == "ACCEPT"]
    metrics = cascade.metrics.payload()
    nano_accepts = sum(row["nano_decision"] == "ACCEPT" for row in rows)
    nano_rejects = sum(row["nano_decision"] == "REJECT" for row in rows)
    nano_escalates = len(rows) - nano_accepts - nano_rejects
    new_accepts = sum(row["new_final_decision"] == "ACCEPT" for row in rows)
    return {
        "schema_version": "cost_first_classifier_replay_v1",
        "TOTAL CANDIDATES": len(rows),
        "BASELINE ACCEPT": len(positives),
        "BASELINE REJECT": len(negatives),
        "NANO ACCEPT": nano_accepts,
        "NANO REJECT": nano_rejects,
        "NANO ESCALATE_TO_LUNA": nano_escalates,
        "LUNA ESCALATION CALLS": sum(bool(row["luna_invoked"]) for row in rows),
        "LUNA FINAL ACCEPT": sum(row["luna_invoked"] and row["new_final_decision"] == "ACCEPT" and not row["accepted_due_to_classifier_failure"] for row in rows),
        "LUNA FINAL REJECT": sum(row["luna_invoked"] and row["new_final_decision"] == "REJECT" for row in rows),
        "LUNA OPERATIONAL-FAIL ACCEPT": sum(bool(row["accepted_due_to_classifier_failure"]) for row in rows),
        "FINAL NEW ACCEPT": new_accepts,
        "FINAL NEW REJECT": len(rows) - new_accepts,
        "BASELINE ACCEPT LOST": len(lost),
        "FALSE NEGATIVES": len(lost),
        "OLD REJECT -> NEW ACCEPT": len(gained),
        "NANO RESOLUTION RATE": metrics["nano_resolution_rate"],
        "LUNA ESCALATION RATE": metrics["luna_escalation_rate"],
        "baseline_accept_lost": lost,
        "old_reject_new_accept": gained,
        "stage_metrics": metrics,
        "rows": rows,
    }


def write_replay_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path
