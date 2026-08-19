from __future__ import annotations

import json
from datetime import UTC, datetime

from companyk_newsbot.downstream_replay import load_enriched_articles
from companyk_newsbot.forensic_runner import _latest_journal
from companyk_newsbot.full_shadow_artifacts import journal_collection_data
from companyk_newsbot.models import Article


def test_enrichment_journal_preserves_model_ready_article_for_replay(tmp_path) -> None:
    article = Article(source="fixture", source_type="rss", title="Alpha funding", url="https://example.test/a",
        canonical_url="https://example.test/a", description="description", text="enriched full text",
        published_at=datetime(2026, 8, 19, tzinfo=UTC), retrieved_at=datetime(2026, 8, 19, tzinfo=UTC),
        origin_metadata={"origin_queries": ["Alpha"], "candidate_company_ids": ["company-alpha"], "enrichment_status": "success"})
    path = tmp_path / "shadow.json"
    path.write_text(json.dumps({"stages": {"enrichment": journal_collection_data([article], enrichment_seconds=1.0)}}), encoding="utf-8")
    restored = load_enriched_articles(path)
    assert restored == [article]
    assert restored[0].origin_metadata["candidate_company_ids"] == ["company-alpha"]


def test_forensic_runner_reads_last_atomic_journal_stage(tmp_path) -> None:
    journal = tmp_path / "full_shadow_20260819T000000Z.json"
    journal.write_text(json.dumps({"run_status": "running", "last_completed_stage": "enrichment"}), encoding="utf-8")
    (tmp_path / "full_shadow_20260819T000000Z.runtime.json").write_text(json.dumps({"status": "running"}), encoding="utf-8")
    assert _latest_journal(tmp_path) == {"run_status": "running", "last_completed_stage": "enrichment"}
