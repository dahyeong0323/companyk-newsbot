from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from companyk_newsbot.judges import CascadeSettings, LunaJudgeOutput, RouteBCascadeJudge
from companyk_newsbot.editorial_replay import load_editorial_replay_bundle
from companyk_newsbot.replay import run_replay


class Responses:
    async def parse(self, **_kwargs):
        return SimpleNamespace(output_parsed=LunaJudgeOutput(decision="ACCEPT", reason_code="none", short_reason="clear", confidence="high", event_family="policy", materiality="high", impact_direction="negative", causal_mechanism="causal"), usage=None)


class Client:
    responses = Responses()


def test_replay_compares_luna_to_stored_sol_without_sol_calls(tmp_path: Path) -> None:
    artifact = {"debug":{"route_b":{"judgments":[{"company":"Example Co","article":{"title":"Article","source":"Source","url":"https://example.com","published_at":"2026-08-12T00:00:00+00:00"},"exposure":{"exposure_id":"example","subject":"Example","allowed_event_families":["policy"]},"judge":{"qualifies":False}}]}}}
    source = tmp_path / "baseline.json"; source.write_text(json.dumps(artifact), encoding="utf-8")
    client = Client(); cascade = RouteBCascadeJudge(client, client, CascadeSettings(luna_rpm_budget=1000, sol_rpm_budget=1000))
    report = asyncio.run(run_replay(source, judge=cascade))
    assert report["old_sol_rejects_accepted_by_luna"]


def test_editorial_replay_loader_restores_sha_verified_chunks(tmp_path: Path) -> None:
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
    ]), encoding="utf-8")
    assert load_editorial_replay_bundle(path) == bundle
