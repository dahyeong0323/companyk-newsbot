from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from companyk_newsbot.editorial_replay import load_editorial_replay_bundle
from companyk_newsbot.judges import CascadeSettings, NanoJudgeOutput, RouteBCascadeJudge
from companyk_newsbot.replay import ReplayError, run_replay


class Responses:
    async def parse(self, **_kwargs):
        return SimpleNamespace(
            output_parsed=NanoJudgeOutput(decision="ACCEPT", reason_code="MATERIAL_LINK|policy|high|negative"),
            usage=None,
        )


class Client:
    responses = Responses()


def judgment(*, exact: bool = True) -> dict:
    article = {
        "title": "Article", "source": "Source", "url": "https://example.com",
        "published_at": "2026-08-12T00:00:00+00:00",
    }
    if exact:
        article.update({
            "source_type": "google_news_rss", "retrieved_at": datetime.now(UTC).isoformat(),
            "description": "A policy event", "text": "Policy text", "origin_metadata": {"query": "policy"},
        })
    return {
        "company": "Example Co", "article": article,
        "exposure": {"exposure_id": "example", "subject": "Example", "allowed_event_families": ["policy"]},
        "judge": {"qualifies": False, "audit": {"final_decision": "REJECT"}},
    }


def test_replay_compares_new_final_path_to_frozen_final_decision(tmp_path: Path) -> None:
    artifact = {"debug": {"route_b": {"judgments": [judgment()]}}}
    source = tmp_path / "baseline.json"
    source.write_text(json.dumps(artifact), encoding="utf-8")
    client = Client()
    cascade = RouteBCascadeJudge(
        client, client,
        CascadeSettings(nano_rpm_budget=1000, luna_rpm_budget=1000),
    )
    report = asyncio.run(run_replay(source, judge=cascade))
    assert report["TOTAL CANDIDATES"] == 1
    assert report["OLD REJECT -> NEW ACCEPT"] == 1
    assert report["BASELINE ACCEPT LOST"] == 0


def test_replay_refuses_incomplete_historical_classifier_input(tmp_path: Path) -> None:
    artifact = {"debug": {"route_b": {"judgments": [judgment(exact=False)]}}}
    source = tmp_path / "baseline.json"
    source.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(ReplayError, match="exact classifier input is unrecoverable"):
        asyncio.run(run_replay(source))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_editorial_replay_loader_restores_sha_verified_chunks(tmp_path: Path, encoding: str) -> None:
    bundle = {"schema_version": "editorial_replay_bundle_v1", "run_id": "run-1", "git_commit": "abc", "events": []}
    compressed = gzip.compress(json.dumps(bundle).encode())
    encoded = base64.b64encode(compressed).decode()
    digest = hashlib.sha256(compressed).hexdigest()
    path = tmp_path / "forensic.jsonl"
    path.write_text("\n".join(json.dumps(value) for value in [
        {"event": "shadow_replay_begin", "chunks": 2, "sha256": digest},
        {"event": "shadow_replay_chunk", "seq": 1, "data": encoded[:8]},
        {"event": "shadow_replay_chunk", "seq": 2, "data": encoded[8:]},
        {"event": "shadow_replay_end", "sha256": digest},
    ]), encoding=encoding)
    assert load_editorial_replay_bundle(path) == bundle
