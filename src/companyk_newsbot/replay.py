"""Offline Sol-baseline replay evaluation for the Luna primary judge."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from companyk_newsbot.judges import RouteBCascadeJudge
from companyk_newsbot.models import Article
from companyk_newsbot.rules import RouteBCandidate


class ReplayError(RuntimeError):
    pass


def _candidate(record: dict[str, Any]) -> RouteBCandidate:
    article, exposure = record["article"], record["exposure"]
    published = article.get("published_at")
    return RouteBCandidate(
        article=Article(source=article["source"], source_type="replay_artifact", title=article["title"], url=article["url"], canonical_url=article["url"], description="", published_at=datetime.fromisoformat(published) if published else None, retrieved_at=datetime.now(UTC)),
        company=record["company"], exposure_id=exposure["exposure_id"], exposure_subject=exposure["subject"], allowed_event_families=tuple(exposure["allowed_event_families"]),
    )


async def run_replay(artifact_path: Path, *, judge: RouteBCascadeJudge | None = None) -> dict[str, Any]:
    """Call Luna once per stored candidate; old Sol decision stays a reference only."""
    try:
        records = json.loads(artifact_path.read_text(encoding="utf-8"))["debug"]["route_b"]["judgments"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"invalid full-shadow baseline artifact: {artifact_path}") from exc
    if not isinstance(records, list) or not records:
        raise ReplayError("baseline artifact has no Route B judgments")
    cascade = judge or RouteBCascadeJudge.from_environment()
    candidates = [_candidate(record) for record in records]
    luna = await asyncio.gather(*(cascade._luna(candidate) for candidate in candidates))
    rows = []
    for record, candidate, (output, failure, audit) in zip(records, candidates, luna):
        old = "ACCEPT" if record["judge"]["qualifies"] else "REJECT"
        rows.append({"candidate_id": audit.get("candidate_id"), "company": candidate.company, "article_title": candidate.article.title, "article_url": candidate.article.canonical_url, "old_sol_decision": old, "luna_decision": output.decision if output else "ESCALATE_TO_SOL", "luna_failure": failure, "luna_reason_code": output.reason_code if output else None, "luna_short_reason": output.short_reason if output else None})
    positives = [row for row in rows if row["old_sol_decision"] == "ACCEPT"]
    negatives = [row for row in rows if row["old_sol_decision"] == "REJECT"]
    wrong_rejects = [row for row in positives if row["luna_decision"] == "REJECT"]
    wrong_accepts = [row for row in negatives if row["luna_decision"] == "ACCEPT"]
    escalates = sum(row["luna_decision"] == "ESCALATE_TO_SOL" for row in rows)
    return {"schema_version": "luna_replay_v1", "replay_candidates": len(rows), "sol_reference_accepts": len(positives), "sol_reference_rejects": len(negatives), "luna_accepts": sum(row["luna_decision"] == "ACCEPT" for row in rows), "luna_rejects": sum(row["luna_decision"] == "REJECT" for row in rows), "luna_escalates": escalates, "old_sol_accepts_preserved": sum(row["luna_decision"] in {"ACCEPT", "ESCALATE_TO_SOL"} for row in positives), "old_sol_accepts_rejected_by_luna": wrong_rejects, "old_sol_rejects_accepted_by_luna": wrong_accepts, "disagreements": [*wrong_rejects, *wrong_accepts], "positive_preservation_rate": round((len(positives)-len(wrong_rejects))/len(positives), 5) if positives else None, "direct_agreement_rate": round(sum(row["old_sol_decision"] == row["luna_decision"] for row in rows)/len(rows), 5), "escalation_rate": round(escalates/len(rows), 5), "luna_metrics": cascade.metrics.payload()}


def write_replay_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path
