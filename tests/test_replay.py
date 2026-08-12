from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from companyk_newsbot.judges import CascadeSettings, LunaJudgeOutput, RouteBCascadeJudge
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
