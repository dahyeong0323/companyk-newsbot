"""Replay only the frozen final-editorial corpus from a forensic JSONL log."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from companyk_newsbot.judges import InsightGroundingVerifier, NewsSummarizer


class EditorialReplayError(RuntimeError):
    pass


def load_editorial_replay_bundle(path: Path) -> dict[str, Any]:
    """Restore and integrity-check the gzip/base64 bundle emitted by Full Shadow."""
    try:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        begin = next(value for value in records if value.get("event") == "shadow_replay_begin")
        chunks = sorted((value for value in records if value.get("event") == "shadow_replay_chunk"), key=lambda value: value["seq"])
        end = next(value for value in records if value.get("event") == "shadow_replay_end")
        if [value["seq"] for value in chunks] != list(range(1, begin["chunks"] + 1)):
            raise ValueError("missing or reordered replay chunks")
        compressed = base64.b64decode("".join(value["data"] for value in chunks), validate=True)
        digest = hashlib.sha256(compressed).hexdigest()
        if digest != begin["sha256"] or digest != end["sha256"]:
            raise ValueError("replay bundle sha256 mismatch")
        bundle = json.loads(gzip.decompress(compressed))
    except (OSError, KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EditorialReplayError(f"invalid forensic replay log: {path}") from exc
    if bundle.get("schema_version") != "editorial_replay_bundle_v1" or not isinstance(bundle.get("events"), list):
        raise EditorialReplayError("forensic replay bundle has an invalid schema")
    return bundle


def run_editorial_replay(bundle: dict[str, Any], *, client: Any, summary_model: str, summary_reasoning: str, grounding_model: str, grounding_reasoning: str, concurrency: int = 4) -> dict[str, Any]:
    """Run Sol editor and Luna grounding only; no RSS, routing, ranking, state, or delivery."""
    events = bundle["events"]
    if not events:
        raise EditorialReplayError("forensic replay bundle has no events")

    def replay_one(event: dict[str, Any]) -> dict[str, Any]:
        worker = NewsSummarizer(
            client,
            model=summary_model,
            reasoning_effort=summary_reasoning,
            grounding_verifier=InsightGroundingVerifier(client, model=grounding_model, reasoning_effort=grounding_reasoning),
        )
        try:
            summary = worker.summarize_exact_payload(
                event_id=event["event_id"], route=event["exact_editor_input"]["route"],
                payload=event["exact_editor_input"], evidence_article_ids=set(event["exact_grounding_evidence_article_ids"]),
            )
            return {"event_id": event["event_id"], "rank": event["rank"], "final_status": "success", "summary": summary.model_dump(), "trace": worker.forensic_trace}
        except Exception as exc:
            return {"event_id": event["event_id"], "rank": event["rank"], "final_status": "failed", "exception_type": type(exc).__name__, "exception_message": str(exc), "trace": worker.forensic_trace}

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(replay_one, events))
    return {
        "schema_version": "editorial_replay_result_v1", "source_run_id": bundle["run_id"], "source_git_commit": bundle["git_commit"],
        "events": results, "successes": sum(value["final_status"] == "success" for value in results),
        "failures": sum(value["final_status"] == "failed" for value in results),
    }


def main() -> int:
    import argparse
    from openai import OpenAI

    parser = argparse.ArgumentParser(description="Replay frozen Full Shadow editorial inputs only.")
    parser.add_argument("forensic_jsonl", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    bundle = load_editorial_replay_bundle(args.forensic_jsonl)
    report = run_editorial_replay(
        bundle, client=OpenAI(timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))),
        summary_model=os.getenv("SUMMARY_MODEL", "gpt-5.6-sol"), summary_reasoning=os.getenv("SUMMARY_REASONING", "medium"),
        grounding_model=os.getenv("GROUNDING_MODEL", "gpt-5.6-luna"), grounding_reasoning=os.getenv("GROUNDING_REASONING", "medium"),
        concurrency=int(os.getenv("SUMMARY_CONCURRENCY", "4")),
    )
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"event": "editorial_replay_complete", "output": str(args.output), "successes": report["successes"], "failures": report["failures"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
