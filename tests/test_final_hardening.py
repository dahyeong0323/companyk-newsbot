from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from companyk_newsbot import e2e
from companyk_newsbot.config import KeywordMapConfig
from companyk_newsbot.dedup import EventAnchors, EventResolverOutput, LunaEventPairResolver, RouteAEventClusterer, RouteBEventClusterer, article_id
from companyk_newsbot.dedup.event import deterministic_pair_decision
from companyk_newsbot.judges import GroundingVerifierOutput, InsightGroundingVerifier, JudgeOutput, JudgedRouteBCandidate, NewsSummarizer, SummaryError, SummaryOutput
from companyk_newsbot.models import Article
from companyk_newsbot.email import EmailNewsItem, HtmlEmailRenderer
from companyk_newsbot.full_shadow_artifacts import journal_event_data
from companyk_newsbot.ranking import RankedNewsItem
from companyk_newsbot.rules import RouteAMatch, RouteBCandidate
from companyk_newsbot.state import JsonStateStore
from companyk_newsbot.collectors.google_news_rss import QueryCollectionResult, RSSCollectionResult


NOW = datetime(2026, 8, 11, 8, tzinfo=UTC)


def article(title: str, url: str = "https://example.com/article", hour: int = 8) -> Article:
    return Article(source="Publisher", source_type="fixture", title=title, url=url, canonical_url=url, published_at=NOW.replace(hour=hour), retrieved_at=NOW, description=title)


def pair(left: str, right: str):
    la, ra = EventAnchors.from_article(article(left, "https://a")), EventAnchors.from_article(article(right, "https://b", 9))
    return deterministic_pair_decision(article(left, "https://a"), article(right, "https://b", 9), left_anchors=la, right_anchors=ra, event_window=timedelta(hours=72)), la, ra


@pytest.mark.parametrize(
    ("left", "right", "field", "canonical"),
    [
        ("Acme raises 100억원", "Acme raises 100억 원", "amount_tokens", "KRW:10000000000"),
        ("Acme raises ₩10,000,000,000", "Acme raises KRW 10 billion", "amount_tokens", "KRW:10000000000"),
        ("Acme raises KRW 10 billion", "Acme raises 10bn won", "amount_tokens", "KRW:10000000000"),
        ("Acme raises $10m", "Acme raises $10 million", "amount_tokens", "USD:10000000"),
        ("Acme acquires 10%", "Acme acquires 10 percent", "percentage_tokens", "0.1"),
        ("Acme event 2026-08-10", "Acme event August 10, 2026", "explicit_date_tokens", "2026-08-10"),
    ],
)
def test_equivalent_anchor_formats_have_identical_canonical_values(left: str, right: str, field: str, canonical: str) -> None:
    decision, la, ra = pair(left, right)
    assert getattr(la, field) == getattr(ra, field) == frozenset({canonical})
    assert decision[0] != "DIFFERENT_EVENT"


@pytest.mark.parametrize(
    ("left", "right", "reason"),
    [
        ("에이컴퍼니 100억원 투자 유치", "에이컴퍼니 200억 원 투자유치", "amount_conflict"),
        ("Acme acquires 10%", "Acme acquires 12 pct", "percentage_conflict"),
        ("Acme event 2026-08-10", "Acme event 2026.08.11", "explicit_date_conflict"),
    ],
)
def test_distinct_canonical_values_are_explicit_conflicts(left: str, right: str, reason: str) -> None:
    assert pair(left, right)[0] == ("DIFFERENT_EVENT", reason)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Acme signs partnership with Alpha and Beta", "Acme signs partnership with Alpha and Gamma"),
        ("Acme acquires Alpha under agreement with Beta", "Acme partners with Beta"),
    ],
)
def test_partial_party_or_secondary_action_overlap_is_never_deterministic_same(left: str, right: str) -> None:
    assert pair(left, right)[0][0] != "SAME_EVENT"


def test_missing_anchor_does_not_create_explicit_conflict() -> None:
    assert pair("Acme raises $10 million", "Acme raises funding")[0][0] != "DIFFERENT_EVENT"


def judged(value: Article, company: str = "Acme", exposure: str = "exposure", family: str = "competition") -> JudgedRouteBCandidate:
    candidate = RouteBCandidate(value, company, exposure, "subject", (family,))
    return JudgedRouteBCandidate(candidate, JudgeOutput(qualifies=True, company=company, exposure_id=exposure, event_family=family, materiality="medium", impact_direction="mixed", causal_mechanism="Approved mechanism.", rejection_reason="none"), "test", "test")


def test_exact_identity_collapse_has_explicit_link_audit() -> None:
    shared = article("Platform decision")
    event = RouteBEventClusterer().cluster([judged(shared, "A", "a", "competition"), judged(shared, "B", "b", "policy_regulatory")])[0]
    assert len(event.exact_identity_collapses) == 1
    collapse = event.exact_identity_collapses[0]
    assert collapse.companies == ("A", "B")
    assert collapse.source_event_families == ("competition", "policy_regulatory")
    assert len(collapse.collapsed_link_ids) == 2
    serialized = journal_event_data([], [event])["route_b_events"][0]["exact_identity_collapses"][0]
    assert serialized["companies"] == ["A", "B"]
    assert len(serialized["collapsed_link_ids"]) == 2


class StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class RaisingResponses:
    def __init__(self, error: Exception) -> None:
        self.error = error
    def parse(self, **kwargs):
        raise self.error


@pytest.mark.parametrize(("status", "failure"), [(429, "rate_limit_exhausted"), (500, "server_error_exhausted"), (503, "server_error_exhausted")])
def test_event_resolver_exhausted_http_failures_keep_events_separate(status: int, failure: str) -> None:
    resolver = LunaEventPairResolver(SimpleNamespace(responses=RaisingResponses(StatusError(status))))
    clusterer = RouteAEventClusterer(resolver=resolver)
    events = clusterer.cluster([
        RouteAMatch("Acme", ("Acme",), article("Acme market update", "https://a")),
        RouteAMatch("Acme", ("Acme",), article("Acme market outlook", "https://b", 9)),
    ])
    assert len(events) == 2
    assert clusterer.metrics.luna_event_dedup_failures == 1
    assert next(decision for event in events for decision in event.dedup_decisions).luna_failure_type == failure


def direct_item() -> RankedNewsItem:
    event = RouteAEventClusterer().cluster([RouteAMatch("Acme", ("Acme",), article("Acme routine administrative filing"))])[0]
    return RankedNewsItem.from_direct_event(event)


def summary(item: RankedNewsItem, *, evidence=None, mode="watchpoint", text="확인 포인트: 다음 공시의 구체적 일정과 조건을 확인해야 한다.") -> SummaryOutput:
    return SummaryOutput(fact_summary="Acme가 행정 공시를 제출했다.", insight_one_liner=text, insight_dimension="strategy", insight_mode=mode, confidence="medium", evidence_article_ids=[article_id(item.direct_match.article)] if evidence is None else evidence)


class SequenceResponses:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = []
    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.values.pop(0))


class SupportedVerifier:
    def verify(self, event_payload, proposed):
        return GroundingVerifierOutput(decision="SUPPORTED", short_reason="fixture")


@pytest.mark.parametrize("bad", [[], ["unknown"], ["duplicate", "duplicate"]])
def test_all_evidence_validation_errors_retry_exactly_once(bad: list[str]) -> None:
    item = direct_item()
    responses = SequenceResponses(summary(item, evidence=bad), summary(item))
    summarizer = NewsSummarizer(SimpleNamespace(responses=responses), model="sol", grounding_verifier=SupportedVerifier())
    assert summarizer.summarize(item).evidence_article_ids
    assert len(responses.calls) == 2
    assert summarizer.metrics.validation_retries == 1


def test_missing_evidence_field_uses_same_one_retry_path() -> None:
    item = direct_item()
    missing = SummaryOutput(fact_summary="Fact.", insight_one_liner="확인 포인트: 다음 공시 일정을 확인해야 한다.", insight_dimension="strategy", insight_mode="watchpoint", confidence="medium")
    responses = SequenceResponses(missing, summary(item))
    summarizer = NewsSummarizer(SimpleNamespace(responses=responses), model="sol", grounding_verifier=SupportedVerifier())
    assert summarizer.summarize(item).evidence_article_ids
    assert summarizer.metrics.validation_retries == 1


@pytest.mark.parametrize(("field", "value"), [("insight_mode", "forecast"), ("confidence", "low"), ("insight_dimension", "made_up")])
def test_malformed_insight_enums_are_rejected_by_schema(field: str, value: str) -> None:
    payload = summary(direct_item()).model_dump()
    payload[field] = value
    with pytest.raises(Exception):
        SummaryOutput.model_validate(payload)


def test_invalid_then_invalid_fails_after_exactly_one_retry() -> None:
    item = direct_item()
    responses = SequenceResponses(summary(item, evidence=[]), summary(item, evidence=["unknown"]))
    summarizer = NewsSummarizer(SimpleNamespace(responses=responses), model="sol", grounding_verifier=SupportedVerifier())
    with pytest.raises(SummaryError, match="after one retry"):
        summarizer.summarize(item)
    assert len(responses.calls) == 2


def test_unsupported_implication_is_rewritten_once_as_grounded_watchpoint() -> None:
    item = direct_item()
    implication = summary(item, mode="implication", text="기업가치 상승과 경쟁력 강화가 기대된다.")
    watchpoint = summary(item)
    editor = SequenceResponses(implication, watchpoint)
    verifier_responses = SequenceResponses(
        GroundingVerifierOutput(decision="SUPPORTED", short_reason="core factual fields are grounded"),
        GroundingVerifierOutput(decision="UNSUPPORTED", unsupported_claims=["valuation"], short_reason="not supported"),
        GroundingVerifierOutput(decision="SUPPORTED", short_reason="concrete watchpoint is grounded"),
    )
    summarizer = NewsSummarizer(
        SimpleNamespace(responses=editor), model="gpt-5.6-sol",
        grounding_verifier=InsightGroundingVerifier(SimpleNamespace(responses=verifier_responses), model="gpt-5.6-luna"),
    )
    result = summarizer.summarize(item)
    assert result.insight_mode == "watchpoint"
    assert "기업가치" not in result.insight_one_liner
    assert len(editor.calls) == 2 and len(verifier_responses.calls) == 3
    assert editor.calls[1]["model"] == "gpt-5.6-sol"
    assert verifier_responses.calls[0]["model"] == "gpt-5.6-luna"
    rendered = HtmlEmailRenderer().render([EmailNewsItem(item, result)], report_date=date(2026, 8, 12))
    assert "기업가치 상승" not in rendered.html
    assert result.insight_one_liner in rendered.html


def test_invalid_watchpoint_rewrite_uses_safe_fallback() -> None:
    item = direct_item()
    implication = summary(item, mode="implication", text="기업가치 상승이 기대된다.")
    invalid = summary(item, text="귀추가 주목된다.")
    editor = SequenceResponses(implication, invalid)
    verifier = SimpleNamespace(verify=lambda payload, proposed: GroundingVerifierOutput(decision="UNSUPPORTED", short_reason="unsupported"))
    result = NewsSummarizer(SimpleNamespace(responses=editor), model="sol", grounding_verifier=verifier).summarize(item)
    assert result.insight_mode == "watchpoint"
    assert result.insight_one_liner == "해당 사건의 실제 이행과 관련 공식 발표가 후속 보도에서 확인되는지 주시."


class SplitVerifier:
    def __init__(self, *, core: tuple[str, ...] = ("SUPPORTED",), insights: tuple[str, ...] = ("SUPPORTED",)) -> None:
        self.core = list(core)
        self.insights = list(insights)

    def verify_core(self, event_payload, proposed):
        return GroundingVerifierOutput(decision=self.core.pop(0), short_reason="core fixture")

    def verify(self, event_payload, proposed):
        return GroundingVerifierOutput(decision=self.insights.pop(0), short_reason="insight fixture")


def test_unsupported_core_facts_are_rewritten_once_before_insight_grounding() -> None:
    item = direct_item()
    original = summary(item, text="Monitor the next official update.")
    rewritten = summary(item, text="Monitor the next official update.")
    verifier = SplitVerifier(core=("UNSUPPORTED", "SUPPORTED"))
    summarizer = NewsSummarizer(SimpleNamespace(responses=SequenceResponses(original, rewritten)), model="sol", grounding_verifier=verifier)
    assert summarizer.summarize(item).fact_summary == rewritten.fact_summary
    assert verifier.insights == []
    assert summarizer.metrics.core_rewrites == 1
    assert summarizer.metrics.core_fallbacks == 0
    assert any(trace["event"] == "core_recovery_rewrite" and trace["core_fallback_used"] is False for trace in summarizer.forensic_trace)


def test_second_unsupported_core_rewrite_uses_deterministic_factual_fallback() -> None:
    item = direct_item()
    original = summary(item, text="Monitor the next official update.")
    still_unsupported = summary(item, text="Monitor the next official update.")
    summarizer = NewsSummarizer(
        SimpleNamespace(responses=SequenceResponses(original, still_unsupported)), model="sol",
        grounding_verifier=SplitVerifier(core=("UNSUPPORTED", "UNSUPPORTED", "SUPPORTED")),
    )
    result = summarizer.summarize(item)
    assert result.fact_summary == item.direct_match.article.title
    assert result.why_it_matters is None
    assert summarizer.metrics.core_rewrites == summarizer.metrics.core_fallbacks == 1
    fallback = next(trace for trace in summarizer.forensic_trace if trace["event"] == "core_fallback_used")
    assert fallback["core_original"]["fact_summary"] == original.fact_summary
    assert fallback["core_rewrite"]["fact_summary"] == still_unsupported.fact_summary
    assert fallback["core_fallback_used"] is True


def test_core_fallback_uses_event_related_digest_clause() -> None:
    payload = json.dumps({
        "route": "external", "company": "Portfolio Co",
        "representative_article": {"title": "Other company raises funds; Novo launches a Wegovy subscription; unrelated fraud case"},
        "approved_impact_links": [{"causal_mechanism": "Novo Wegovy subscription could affect the relevant market."}],
    })
    proposed = SummaryOutput(
        fact_summary="Unsafe paraphrase.", why_it_matters="Approved context.", insight_one_liner="Monitor the next update.",
        insight_dimension="competition", insight_mode="watchpoint", confidence="medium", evidence_article_ids=["known"],
    )
    fallback = NewsSummarizer._safe_core_fallback(payload, proposed)
    assert fallback.fact_summary == "Novo launches a Wegovy subscription"


def test_unsupported_watchpoint_is_rewritten_once_and_keeps_event() -> None:
    item = direct_item()
    original = summary(item, text="근거 밖의 구체 조건을 확인해야 한다.")
    rewritten = summary(item, text="후속 공식 발표에서 실제 이행이 확인되는지 주시.")
    summarizer = NewsSummarizer(
        SimpleNamespace(responses=SequenceResponses(original, rewritten)), model="sol",
        grounding_verifier=SplitVerifier(insights=("UNSUPPORTED", "SUPPORTED")),
    )
    result = summarizer.summarize(item)
    assert result.insight_one_liner == rewritten.insight_one_liner
    assert summarizer.metrics.watchpoint_rewrites == 1
    assert summarizer.metrics.watchpoint_fallbacks == 0
    assert any(trace["event"] == "watchpoint_recovery_begin" and trace["watchpoint_original"] == original.insight_one_liner for trace in summarizer.forensic_trace)
    assert any(trace["event"] == "watchpoint_recovery_rewrite" and trace["watchpoint_fallback_used"] is False for trace in summarizer.forensic_trace)


def test_unsupported_watchpoint_rewrite_uses_deterministic_fallback_and_keeps_event() -> None:
    item = direct_item()
    original = summary(item, text="근거 밖의 구체 조건을 확인해야 한다.")
    still_unsupported = summary(item, text="여전히 근거 밖의 조건을 확인해야 한다.")
    summarizer = NewsSummarizer(
        SimpleNamespace(responses=SequenceResponses(original, still_unsupported)), model="sol",
        grounding_verifier=SplitVerifier(insights=("UNSUPPORTED", "UNSUPPORTED")),
    )
    result = summarizer.summarize(item)
    assert result.insight_mode == "watchpoint"
    assert result.insight_one_liner == "해당 사건의 실제 이행과 관련 공식 발표가 후속 보도에서 확인되는지 주시."
    assert summarizer.metrics.watchpoint_fallbacks == 1
    fallback = next(trace for trace in summarizer.forensic_trace if trace["event"] == "watchpoint_fallback_used")
    assert fallback["watchpoint_original"] == original.insight_one_liner
    assert fallback["watchpoint_rewrite"] == still_unsupported.insight_one_liner
    assert fallback["watchpoint_grounding_verdict"] == "UNSUPPORTED"
    assert fallback["watchpoint_fallback_used"] is True


def test_editor_payload_contains_canonical_event_and_family_context() -> None:
    shared = article("Platform penalty $10 million on 2026-08-10")
    event = RouteBEventClusterer().cluster([judged(shared, "A", "a", "competition"), judged(shared, "B", "b", "policy_regulatory")])[0]
    payload, _ = NewsSummarizer._payload(RankedNewsItem.from_external_event(event))
    parsed = json.loads(payload)
    assert parsed["canonical_event_anchors"]["amount_tokens"] == ["USD:10000000"]
    assert parsed["event_family_context"]["source_event_families"] == ["competition", "policy_regulatory"]
    assert {link["company"] for link in parsed["approved_impact_links"]} == {"A", "B"}


def minimal_config() -> KeywordMapConfig:
    return KeywordMapConfig.model_validate({
        "schema_version": "test", "name": "test",
        "external_impact_logic": {"event_families": {"policy": "policy"}, "matching_rules": {"policy": {}}, "query_registry": {}, "causal_judge": {}},
        "company_rules": {"Acme": {"aliases": [], "no_justified_external_exposure": {"status": True, "reason": "test", "review_date": "2026-01-01"}}},
    })


def test_full_shadow_artifact_preflight_fails_before_collector_or_llm(monkeypatch, tmp_path) -> None:
    called = []
    monkeypatch.delenv("ARTIFACT_DIR", raising=False)
    monkeypatch.setattr(e2e, "GoogleNewsRSSCollector", lambda **kwargs: called.append("collector"))
    monkeypatch.setattr(e2e, "LunaEventPairResolver", SimpleNamespace(from_environment=lambda: called.append("luna")))
    with pytest.raises(e2e.E2EExecutionError, match="ARTIFACT_DIR"):
        e2e.run_real_e2e(minimal_config(), JsonStateStore(tmp_path), now=NOW, profile="full_shadow", deliver=False)
    assert called == []


def test_full_shadow_summary_failure_persists_complete_partial_journal(monkeypatch, tmp_path) -> None:
    artifact_dir = tmp_path / "persistent-artifacts"
    monkeypatch.setenv("ARTIFACT_DIR", str(artifact_dir.resolve()))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda **kwargs: SimpleNamespace()))
    values = (
        article("Alpha Co raises funding", "https://example.com/alpha"),
        article("Beta Co raises funding", "https://example.com/beta", 7),
    )

    class Collector:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def collect_many(self, queries):
            return RSSCollectionResult((QueryCollectionResult(tuple(queries)[0], "success", values),))

    class Cascade:
        metrics = SimpleNamespace(payload=lambda: {})
        def judge_all_sync(self, candidates): return []

    class CascadeFactory:
        @staticmethod
        def from_environment(): return Cascade()

    class EventResolverFactory:
        @staticmethod
        def from_environment(): return SimpleNamespace(resolve=lambda left, right: None)

    class Summarizer:
        def __init__(self, *args, **kwargs):
            self.metrics = SimpleNamespace(payload=lambda: {
                "summary_calls": 1, "summary_retries": 0, "summary_evidence_retries": 0,
                "summary_failures": 0, "insight_implication_count": 0, "insight_watchpoint_count": 1,
                "grounding_verifier_calls": 0, "grounding_verifier_failures": 0,
                "unsupported_implications": 0, "watchpoint_rewrites": 0,
            })
        def summarize(self, item):
            if item.company == "Beta Co":
                self.metrics = SimpleNamespace(payload=lambda: {
                    "summary_calls": 2, "summary_retries": 1, "summary_evidence_retries": 1,
                    "summary_failures": 1, "insight_implication_count": 0, "insight_watchpoint_count": 0,
                    "grounding_verifier_calls": 0, "grounding_verifier_failures": 0,
                    "unsupported_implications": 0, "watchpoint_rewrites": 0,
                })
                raise SummaryError("summary validation failed after one retry")
            return SummaryOutput(
                fact_summary="Alpha funding fact.",
                insight_one_liner="확인 포인트: 다음 자금 집행 공시를 확인해야 한다.",
                insight_dimension="financing_runway",
                insight_mode="watchpoint",
                confidence="medium",
                evidence_article_ids=[article_id(item.direct_match.article)],
            )

    config = KeywordMapConfig.model_validate({
        "schema_version": "test", "name": "test",
        "external_impact_logic": {"event_families": {"policy": "policy"}, "matching_rules": {"policy": {}}, "query_registry": {}, "causal_judge": {}},
        "company_rules": {
            "Alpha Co": {"aliases": [], "no_justified_external_exposure": {"status": True, "reason": "test", "review_date": "2026-01-01"}},
            "Beta Co": {"aliases": [], "no_justified_external_exposure": {"status": True, "reason": "test", "review_date": "2026-01-01"}},
        },
    })
    monkeypatch.setattr(e2e, "GoogleNewsRSSCollector", Collector)
    monkeypatch.setattr(e2e, "RouteBCascadeJudge", CascadeFactory)
    monkeypatch.setattr(e2e, "LunaEventPairResolver", EventResolverFactory)
    monkeypatch.setattr(e2e, "NewsSummarizer", Summarizer)

    with pytest.raises(e2e.E2EExecutionError, match="final summary failed"):
        e2e.run_real_e2e(config, JsonStateStore(tmp_path / "state"), now=NOW, profile="full_shadow", deliver=False)

    artifacts = list(artifact_dir.glob("full_shadow_*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert payload["run_status"] == "failed"
    assert payload["last_completed_stage"] == "summary"
    assert payload["failed_item_ids"]
    assert {"collection", "qualification", "event_dedup", "ranking", "editorial_replay_bundle", "summary"}.issubset(payload["stages"])
    bundle = payload["stages"]["editorial_replay_bundle"]
    assert bundle["schema_version"] == "editorial_replay_bundle_v1"
    assert bundle["events"][0]["exact_editor_input"]["event_id"]
    assert payload["stages"]["summary"]["successful_items"][0]["event_id"]
    assert payload["stages"]["summary"]["failed_items"][0]["event_id"]
