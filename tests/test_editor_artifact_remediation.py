from __future__ import annotations

from datetime import UTC, date, datetime
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from companyk_newsbot.dedup import RouteAEventClusterer, RouteBEventClusterer, article_id
from companyk_newsbot.email import EmailNewsItem, HtmlEmailRenderer
from companyk_newsbot.full_shadow_artifacts import write_full_shadow_artifacts
from companyk_newsbot.judges import GroundingVerifierOutput, JudgeOutput, JudgedRouteBCandidate, NewsSummarizer, SummaryError, SummaryOutput
from companyk_newsbot.models import Article
from companyk_newsbot.ranking import RankedNewsItem
from companyk_newsbot.rules import RouteAMatch, RouteBCandidate


NOW = datetime(2026, 8, 11, 8, tzinfo=UTC)


def article(title: str, url: str, *, source: str = "Publisher", hour: int = 8, text: str | None = None) -> Article:
    return Article(
        source=source,
        source_type="fixture",
        title=title,
        url=url,
        canonical_url=url,
        published_at=NOW.replace(hour=hour),
        retrieved_at=datetime(2026, 8, 12, tzinfo=UTC),
        description=f"Evidence for {title}",
        text=text,
    )


def judged(value: Article, company: str, exposure: str, materiality: str = "medium") -> JudgedRouteBCandidate:
    candidate = RouteBCandidate(value, company, exposure, "subject", ("competition",))
    decision = JudgeOutput(
        qualifies=True,
        company=company,
        exposure_id=exposure,
        event_family="competition",
        materiality=materiality,
        impact_direction="negative",
        causal_mechanism=f"Approved mechanism for {company}.",
        rejection_reason="none",
    )
    return JudgedRouteBCandidate(candidate, decision, "test", "test")


def direct_item() -> RankedNewsItem:
    values = [
        RouteAMatch("Acme", ("Acme",), article("Acme raises $10 million from Alpha on 2026-08-10", "https://one.example/a", text="Detailed body. " * 20)),
        RouteAMatch("Acme", ("Acme",), article("On 2026-08-10 Acme funding is $10 million from Alpha", "https://two.example/b", hour=9)),
    ]
    return RankedNewsItem.from_direct_event(RouteAEventClusterer().cluster(values)[0])


def external_item() -> RankedNewsItem:
    values = [
        judged(article("Platform penalty of $10 million on 2026-08-10", "https://one.example/event", text="Detailed penalty body. " * 20), "A", "a", "medium"),
        judged(article("Regulator imposes $10 million platform penalty on 2026-08-10", "https://two.example/event", hour=9), "B", "b", "high"),
    ]
    return RankedNewsItem.from_external_event(RouteBEventClusterer().cluster(values)[0])


def output(item: RankedNewsItem, *, evidence: list[str] | None = None, mode: str = "watchpoint") -> SummaryOutput:
    representative = item.direct_match.article if item.direct_match else item.external_match.candidate.article
    return SummaryOutput(
        fact_summary="Grounded factual summary.",
        why_it_matters="Approved portfolio impact." if item.route == "external" else None,
        insight_one_liner="Monitor the next disclosed milestone." if mode == "watchpoint" else "The evidence supports a concrete implication.",
        insight_dimension="strategy",
        insight_mode=mode,
        confidence="medium",
        evidence_article_ids=evidence or [article_id(representative)],
    )


class SequentialResponses:
    def __init__(self, *values: object) -> None:
        self.values = list(values)
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.values.pop(0))


class FailingResponses:
    def parse(self, **kwargs: object) -> object:
        raise RuntimeError("structured output unavailable")


class SupportedVerifier:
    def verify(self, event_payload, proposed):
        return GroundingVerifierOutput(decision="SUPPORTED", short_reason="fixture")


def test_route_a_editor_payload_has_representative_and_corroborating_evidence() -> None:
    item = direct_item()
    responses = SequentialResponses(output(item))
    NewsSummarizer(SimpleNamespace(responses=responses), model="test", grounding_verifier=SupportedVerifier()).summarize(item)
    payload = json.loads(responses.calls[0]["input"][1]["content"])
    assert payload["event_id"] == item.event_id
    assert payload["representative_article"]["article_id"] == article_id(item.direct_match.article)
    assert len(payload["corroborating_articles"]) == 1


def test_route_b_editor_payload_has_complete_event_and_all_impact_links() -> None:
    item = external_item()
    responses = SequentialResponses(output(item))
    NewsSummarizer(SimpleNamespace(responses=responses), model="test", grounding_verifier=SupportedVerifier()).summarize(item)
    payload = json.loads(responses.calls[0]["input"][1]["content"])
    assert payload["impacted_companies"] == ["A", "B"]
    assert payload["event_materiality"] == "high"
    assert {link["company"] for link in payload["approved_impact_links"]} == {"A", "B"}
    assert all("exposure_id" not in link for link in payload["approved_impact_links"])


def test_unknown_evidence_id_retries_once_then_succeeds() -> None:
    item = direct_item()
    responses = SequentialResponses(output(item, evidence=["unknown"]), output(item))
    summarizer = NewsSummarizer(SimpleNamespace(responses=responses), model="test", grounding_verifier=SupportedVerifier())
    assert summarizer.summarize(item).evidence_article_ids == output(item).evidence_article_ids
    assert len(responses.calls) == 2
    assert summarizer.metrics.evidence_retries == 1
    assert "CORRECTION" in responses.calls[1]["input"][1]["content"]


def test_second_invalid_evidence_id_is_safe_failure() -> None:
    item = direct_item()
    responses = SequentialResponses(output(item, evidence=["unknown"]), output(item, evidence=["still-unknown"]))
    summarizer = NewsSummarizer(SimpleNamespace(responses=responses), model="test", grounding_verifier=SupportedVerifier())
    with pytest.raises(SummaryError, match="unknown or unsupplied"):
        summarizer.summarize(item)
    assert len(responses.calls) == 2
    assert summarizer.metrics.evidence_retries == summarizer.metrics.failures == 1


def test_editor_client_or_schema_failure_is_counted_and_fails_safely() -> None:
    summarizer = NewsSummarizer(SimpleNamespace(responses=FailingResponses()), model="test")
    with pytest.raises(SummaryError, match="failed safely"):
        summarizer.summarize(direct_item())
    assert summarizer.metrics.calls == summarizer.metrics.failures == 1


@pytest.mark.parametrize("missing", [{"insight_one_liner": ""}])
def test_summary_schema_rejects_missing_critical_grounding_fields(missing: dict[str, object]) -> None:
    values: dict[str, object] = {
        "fact_summary": "Fact.",
        "insight_one_liner": "Watch the milestone.",
        "insight_dimension": "strategy",
        "insight_mode": "watchpoint",
        "confidence": "medium",
        "evidence_article_ids": ["known"],
    }
    values.update(missing)
    with pytest.raises(ValidationError):
        SummaryOutput.model_validate(values)


@pytest.mark.parametrize("mode", ["watchpoint", "implication"])
def test_explicit_insight_modes_are_valid_and_counted(mode: str) -> None:
    item = direct_item()
    class Supported:
        def verify(self, event_payload, proposed):
            return GroundingVerifierOutput(decision="SUPPORTED", short_reason="fixture")
    summarizer = NewsSummarizer(SimpleNamespace(responses=SequentialResponses(output(item, mode=mode))), model="test", grounding_verifier=Supported())
    result = summarizer.summarize(item)
    assert result.insight_mode == mode
    assert getattr(summarizer.metrics, f"{mode}_count") == 1


def test_watchpoint_fallback_is_concrete_and_prompted() -> None:
    item = direct_item()
    result = output(item, mode="watchpoint")
    responses = SequentialResponses(result)
    assert NewsSummarizer(SimpleNamespace(responses=responses), model="test", grounding_verifier=SupportedVerifier()).summarize(item).insight_one_liner == "Monitor the next disclosed milestone."
    system_prompt = responses.calls[0]["input"][0]["content"]
    assert "watchpoint" in system_prompt and "Never invent" in system_prompt


def test_html_renders_insight_coverage_representative_url_and_multi_company_label() -> None:
    item = external_item()
    rendered = HtmlEmailRenderer().render([EmailNewsItem(item, output(item))], report_date=date(2026, 8, 12))
    assert "Monitor the next disclosed milestone." in rendered.html
    assert "외 1개 매체 보도" in rendered.html
    assert f'href="{item.article_url}"' in rendered.html
    assert "영향: A · B" in rendered.html


def test_full_shadow_artifact_serializes_event_editor_and_dedup_audit(tmp_path) -> None:
    direct = direct_item()
    external = external_item()
    email_items = [
        EmailNewsItem(direct, output(direct), summary_retry_count=1),
        EmailNewsItem(external, output(external)),
    ]
    rendered = HtmlEmailRenderer().render(email_items, report_date=date(2026, 8, 12))
    json_path, html_path = write_full_shadow_artifacts(
        artifact_dir=tmp_path,
        run_time=NOW,
        metrics={"luna_event_dedup_calls": 2, "luna_event_dedup_failures": 1, "duplicate_event_reduction_rate": 0.5},
        delivery_checkpoint_before="2026-08-10T00:00:00+00:00",
        rendered=rendered,
        email_items=email_items,
        route_a_events=[direct.direct_event],
        route_b_events=[external.external_event],
        judged=list(external.external_event.impact_links),
        prefilter_rejections=[],
    )
    payload = json.loads(open(json_path, encoding="utf-8").read())
    route_a = payload["debug"]["route_a_events"][0]
    route_b = payload["debug"]["route_b"]["events"][0]
    final = payload["user_facing"]["final_items"]
    assert all(member["representative_score"] == member["score_breakdown"]["total"] for member in route_a["member_articles"])
    assert route_a["pairwise_dedup_decisions"]
    assert all(member["score_breakdown"]["total"] == member["representative_score"] for member in route_b["member_articles"])
    assert route_b["aggregate_materiality"] == "high"
    assert len(route_b["impact_links"]) == 2
    assert final[0]["summary_evidence_retry_count"] == 1
    assert final[0]["summary_failure"] is False
    assert final[1]["impacted_companies"] == ["A", "B"]
    assert payload["debug"]["metrics"]["luna_event_dedup_failures"] == 1
    assert open(html_path, encoding="utf-8").read() == rendered.html
