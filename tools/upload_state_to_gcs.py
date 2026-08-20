"""Safely copy a validated exported state file into a GCS object at cutover."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from companyk_newsbot.state import RunState


def _validate_run_state(payload: object) -> RunState:
    if not isinstance(payload, dict):
        raise TypeError("state must be a JSON object")
    state = RunState(**payload)
    fingerprints = (state.sent_article_fingerprints, state.sent_event_fingerprints)
    checkpoints = (state.last_successful_run, state.last_successful_delivery_run, state.last_shadow_run)
    if any(not isinstance(values, list) or any(not isinstance(value, str) for value in values) for values in fingerprints):
        raise TypeError("fingerprints must be list[str]")
    if any(value is not None and not isinstance(value, str) for value in checkpoints):
        raise TypeError("checkpoints must be str or null")
    if not isinstance(state.run_ledger, list) or any(not isinstance(entry, dict) for entry in state.run_ledger):
        raise TypeError("run_ledger must be list[dict]")
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_file", type=Path, help="Exported Railway state JSON; it is never modified")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--object", dest="object_name", default="production/newsbot_state.json")
    parser.add_argument("--force", action="store_true", help="Replace an existing destination using its current generation")
    args = parser.parse_args()

    try:
        payload = args.state_file.read_text(encoding="utf-8")
        state = _validate_run_state(json.loads(payload))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise SystemExit(f"Invalid state file: {args.state_file}") from exc
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise SystemExit("google-cloud-storage is required") from exc

    blob = storage.Client().bucket(args.bucket).blob(args.object_name)
    if blob.exists():
        if not args.force:
            raise SystemExit("Destination already exists; inspect it and use --force only for an intentional replacement")
        generation = blob.generation
        if generation is None:
            raise SystemExit("Destination exists but has no object generation; refusing to overwrite")
        expected_generation = int(generation)
    else:
        expected_generation = 0
    blob.upload_from_string(
        payload,
        content_type="application/json; charset=utf-8",
        if_generation_match=expected_generation,
    )
    print(
        json.dumps(
            {
                "destination": f"gs://{args.bucket}/{args.object_name}",
                "article_fingerprints": len(state.sent_article_fingerprints),
                "event_fingerprints": len(state.sent_event_fingerprints),
                "last_successful_delivery_run": state.last_successful_delivery_run,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
