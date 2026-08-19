"""Replay model-only Route A processing from a persisted enrichment journal."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
from time import monotonic
from typing import Any

from companyk_newsbot.full_shadow_artifacts import journal_event_data
from companyk_newsbot.judges.direct_event import DirectEventGrounder, DirectEventJudge
from companyk_newsbot.models import Article
from companyk_newsbot.portfolio_registry import PortfolioRegistry, load_portfolio_registry
from companyk_newsbot.route_a_only import process_route_a_articles
from companyk_newsbot.runtime_progress import RuntimeProgress
from companyk_newsbot.semantic_grouping import GPT54MiniGroupingProvider
from companyk_newsbot.semantic_identity import GPT54MiniIdentityProvider


def load_enriched_articles(artifact_path: Path) -> list[Article]:
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    stages = payload.get("stages") or payload.get("debug", {}).get("journal_stages", {})
    rows = stages.get("enrichment", {}).get("articles") if isinstance(stages, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("artifact has no complete enrichment article snapshot")
    articles: list[Article] = []
    for row in rows:
        value = dict(row)
        value.pop("article_id", None)
        # Pre-forensic artifacts exposed only a canonical URL. They remain
        # replayable, while new artifacts preserve both current Article fields.
        value.setdefault("canonical_url", value.get("url"))
        articles.append(Article.model_validate(value))
    return articles


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        temporary = Path(handle.name)
    temporary.replace(path)


def replay(artifact_path: Path, *, output_path: Path, registry: PortfolioRegistry) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    started = monotonic()
    output: dict[str, Any] = {"status": "running", "started_at": started_at.isoformat(), "last_stage": "load_enrichment"}
    _atomic_json(output_path, output)
    progress = RuntimeProgress(output_path.with_suffix(".runtime.json"))
    try:
        articles = load_enriched_articles(artifact_path)
        output.update({"enriched_articles": len(articles), "last_stage": "model_pipeline"})
        _atomic_json(output_path, output)
        identity = GPT54MiniIdentityProvider.from_environment()
        grouping = GPT54MiniGroupingProvider.from_environment()
        judge = DirectEventJudge.from_environment()
        grounder = DirectEventGrounder.from_environment()
        processed = process_route_a_articles(articles, registry, judge=judge, grounder=grounder,
            identity_provider=identity, grouping_provider=grouping, forensic_progress=progress.event)
        output.update({
            "status": "success", "last_stage": "complete", "events": len(processed.events),
            "email_items": len(processed.email_items), "systemic_model_failure": processed.systemic_model_failure,
            "model_failure_events": processed.model_failure_events, "model_metrics": processed.model_metrics,
            "provider_metrics": {"identity": identity.metrics_payload(), "grouping": grouping.metrics_payload(),
                "materiality": judge.metrics.payload("direct_assessment"), "grounding": grounder.metrics.payload("direct_grounding")},
            "events_audit": journal_event_data(processed.events, []),
            "assessments": {key: value.model_dump() for key, value in processed.assessments.items()},
            "grounding": {key: value.model_dump() for key, value in processed.grounding_verdicts.items()},
        })
        progress.finish("success" if not processed.systemic_model_failure else "inconclusive")
    except Exception as exc:
        output.update({"status": "failed", "last_stage": output.get("last_stage"), "error": repr(exc)})
        progress.finish("failed")
        raise
    finally:
        ended_at = datetime.now(UTC)
        output.update({"ended_at": ended_at.isoformat(), "elapsed_seconds": round(monotonic() - started, 3)})
        _atomic_json(output_path, output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("config/portfolio_registry.yaml"))
    args = parser.parse_args()
    result = replay(args.artifact, output_path=args.output, registry=load_portfolio_registry(args.registry))
    print(json.dumps({"status": result["status"], "output": str(args.output), "elapsed_seconds": result["elapsed_seconds"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
