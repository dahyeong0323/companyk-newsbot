from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from companyk_newsbot import e2e
from companyk_newsbot.collectors.google_news_rss import QueryCollectionResult, RSSCollectionResult
from companyk_newsbot.config import KeywordMapConfig
from companyk_newsbot.judges import SummaryOutput
from companyk_newsbot.models import Article
from companyk_newsbot.state import JsonStateStore


NOW = datetime(2026, 8, 12, 5, tzinfo=UTC)


def config() -> KeywordMapConfig:
    return KeywordMapConfig.model_validate(
        {
            "schema_version": "test",
            "name": "test",
            "external_impact_logic": {
                "event_families": {"policy": "policy"},
                "matching_rules": {"policy": {}},
                "query_registry": {},
                "causal_judge": {},
            },
            "company_rules": {
                "Direct Co": {
                    "aliases": ["Direct"],
                    "no_justified_external_exposure": {
                        "status": True,
                        "reason": "test",
                        "review_date": "2026-01-01",
                    },
                }
            },
        }
    )


def article() -> Article:
    return Article(
        source="Google News",
        source_type="google_news_rss",
        title="Direct Co raises new funding",
        url="https://example.com/direct",
        canonical_url="https://example.com/direct",
        published_at=NOW,
        retrieved_at=NOW,
        description="Direct Co announced a funding round.",
        origin_metadata={"query": "Direct Co"},
    )


class FakeCollector:
    articles: tuple[Article, ...] = ()
    observed_hints: list[str | None] = []

    def __init__(self, *, freshness_hint: str | None = None) -> None:
        self.observed_hints.append(freshness_hint)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def collect_many(self, queries):
        query = tuple(queries)[0]
        return RSSCollectionResult((QueryCollectionResult(query, "success", self.articles),))


class FakeJudge:
    @staticmethod
    def from_environment(*, timeout):
        return SimpleNamespace(judge=lambda candidate: None)


def configure_safe_environment(monkeypatch) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "test-secret")
    monkeypatch.setenv("NEWSBOT_RECIPIENT", e2e.TEST_RECIPIENT)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-secret")
    monkeypatch.setattr(e2e, "GoogleNewsRSSCollector", FakeCollector)
    monkeypatch.setattr(e2e, "RouteBCausalMaterialityJudge", FakeJudge)


def test_zero_item_smoke_is_inconclusive_and_skips_email(monkeypatch, tmp_path) -> None:
    configure_safe_environment(monkeypatch)
    FakeCollector.articles = ()
    sent = []
    monkeypatch.setattr(e2e, "ResendEmailSender", lambda settings: sent.append(settings))

    result = e2e.run_real_e2e(config(), JsonStateStore(tmp_path), now=NOW)

    assert result.status == "inconclusive"
    assert result.final_items == 0
    assert result.delivery_id is None
    assert sent == []
    assert FakeCollector.observed_hints[-1] == "when:7d"


def test_nonempty_smoke_summarizes_and_delivers_only_to_test_recipient(monkeypatch, tmp_path) -> None:
    configure_safe_environment(monkeypatch)
    monkeypatch.delenv("SUMMARY_MODEL", raising=False)
    monkeypatch.delenv("SUMMARY_REASONING", raising=False)
    FakeCollector.articles = (article(),)
    recipients = []
    summary_settings = []

    class FakeSummarizer:
        def __init__(self, *args, **kwargs):
            summary_settings.append((kwargs["model"], kwargs["reasoning_effort"]))
            self.metrics = SimpleNamespace(payload=lambda: {
                "summary_calls": 1, "summary_retries": 0, "summary_evidence_retries": 0, "summary_failures": 0,
                "insight_implication_count": 0, "insight_watchpoint_count": 1, "grounding_verifier_calls": 0,
                "grounding_verifier_failures": 0, "unsupported_implications": 0, "watchpoint_rewrites": 0,
            })

        def summarize(self, item):
            source = item.direct_match.article if item.direct_match else item.external_match.candidate.article
            from companyk_newsbot.dedup import article_id
            return SummaryOutput(fact_summary="투자 유치 소식입니다.", insight_one_liner="자금 집행이 다음 확인 변수입니다.", insight_dimension="financing_runway", insight_mode="watchpoint", confidence="medium", evidence_article_ids=[article_id(source)])

    class FakeSender:
        def __init__(self, settings):
            recipients.append(settings.recipient)

        def send(self, rendered):
            return "delivery-test-id"

        def close(self):
            pass

    monkeypatch.setattr(e2e, "NewsSummarizer", FakeSummarizer)
    monkeypatch.setattr(e2e, "ResendEmailSender", FakeSender)

    result = e2e.run_real_e2e(config(), JsonStateStore(tmp_path), now=NOW)

    assert result.status == "success"
    assert result.freshness_accepted == 1
    assert result.route_a_events == 1
    assert result.final_items == 1
    assert result.summary_calls == 1
    assert result.delivery_id == "delivery-test-id"
    assert recipients == [e2e.TEST_RECIPIENT]
    assert summary_settings == [("gpt-5.6-sol", "medium")]

def test_editorial_trace_log_preserves_nested_event_without_collision(monkeypatch) -> None:
    import companyk_newsbot.e2e as e2e

    emitted = []
    monkeypatch.setattr(e2e, "_log", lambda event, **fields: emitted.append((event, fields)))
    original = {"event": "grounding_verification", "event_id": "event-1"}
    e2e._emit_editorial_traces("run-1", [original])
    assert emitted == [("editorial_trace", {"run_id": "run-1", "trace_event": "grounding_verification", "event_id": "event-1"})]
    assert original["event"] == "grounding_verification"
