from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companyk_newsbot.dedup import article_id
from companyk_newsbot.model_first import prepare_events
from companyk_newsbot.models import Article
from companyk_newsbot.portfolio_registry import load_portfolio_registry
from companyk_newsbot.rules import RouteAMatch
from companyk_newsbot.semantic_grouping import EventCandidate, EventGroup, validate_partition
from companyk_newsbot.semantic_identity import GPT54MiniIdentityProvider, IDENTITY_SYSTEM_PROMPT, IdentityVerdict, _Item, _Response


NOW = datetime(2026, 8, 19, tzinfo=UTC)


def article(title: str) -> Article:
    token = str(abs(hash(title)))
    return Article(source="fixture", source_type="rss", title=title, url=f"https://example.test/{token}",
        canonical_url=f"https://example.test/{token}", published_at=NOW, retrieved_at=NOW, description=title)


class Identity:
    def classify_many(self, *, articles, **kwargs):
        return {item["article_id"]: (IdentityVerdict.RELATED if "업스테이지" in item["title"] else IdentityVerdict.NOT_RELATED) for item in articles}


class Grouping:
    def partition(self, *, candidates, **kwargs):
        return (EventGroup(tuple(item.article_id for item in candidates), candidates[0].article_id, "독파모 평가 결과"),)


class AllRelated:
    def classify_many(self, *, articles, **kwargs):
        return {item["article_id"]: IdentityVerdict.RELATED for item in articles}


@pytest.mark.parametrize("title", [
    "봉선화 씨앗을 심는 법", "평화의 씨앗 캠페인", "식중독균이 빠르게 자란다",
    "서남해 해상 풍력 사업", "마이크로컨텍솔 부품 공급", "Notta 전사 서비스 출시",
])
def test_identity_contract_carries_generic_name_false_positive_context(title: str) -> None:
    """These fixtures are model-evaluation cases, never Python rejection rules."""
    payload = {"article_id": "fixture", "title": title, "lead": title, "publisher": "fixture",
               "publisher_domain": "example.test", "published_at": NOW.isoformat(), "origin_queries": ["씨앗"]}
    class Responses:
        def parse(self, **kwargs):
            return type("Response", (), {"output_parsed": _Response(articles=[_Item(article_id="fixture", decision="NOT_RELATED", confidence="high", reason_code="different_entity")])})()
    provider = GPT54MiniIdentityProvider(type("Client", (), {"responses": Responses()})())
    result = provider.classify_many(company="씨앗", aliases=["씨앗"], registry_context="seed company", articles=[payload])
    assert result["fixture"].verdict == IdentityVerdict.NOT_RELATED
    assert result["fixture"].reason_code == "different_entity"


def test_identity_prompt_explicitly_defends_required_ambiguous_name_cases() -> None:
    for phrase in ("봉선화", "평화", "식중독균", "서남해", "마이크로컨텍솔", "Notta transcription"):
        assert phrase in IDENTITY_SYSTEM_PROMPT


def test_model_grouping_fixture_keeps_milestones_separate_without_pairwise_logic() -> None:
    registry = load_portfolio_registry("config/portfolio_registry.yaml")
    values = [article("업스테이지 독파모 2차 평가 통과"), article("업스테이지 독파모 최종 3파전"),
              article("업스테이지 NVIDIA B200 1000장 지원"), article("업스테이지 KT 모두의 AI 컨소시엄")]

    class FixtureGrouping:
        def partition(self, *, candidates, **kwargs):
            return (
                EventGroup((candidates[0].article_id, candidates[1].article_id), candidates[0].article_id, "독파모 평가 발표", "same announcement", "direct report"),
                EventGroup((candidates[2].article_id,), candidates[2].article_id, "B200 지원"),
                EventGroup((candidates[3].article_id,), candidates[3].article_id, "KT 컨소시엄"),
            )

    events, _ = prepare_events(tuple(RouteAMatch("업스테이지", ("업스테이지",), value) for value in values), registry,
        identity_provider=AllRelated(), grouping_provider=FixtureGrouping())
    assert [event.coverage_count for event in events] == [2, 1, 1]
    assert len({event.semantic_fingerprint for event in events}) == 3
    assert events[0].event_label == "독파모 평가 발표"
    assert events[0].representative_selection_reason == "direct report"


def test_invalid_grouping_retries_once_then_preserves_articles_as_singletons() -> None:
    registry = load_portfolio_registry("config/portfolio_registry.yaml")
    value = article("업스테이지 실제 발표")

    class InvalidGrouping:
        calls = 0
        def partition(self, **kwargs):
            self.calls += 1
            return ()

    provider = InvalidGrouping()
    events, metrics = prepare_events((RouteAMatch("업스테이지", ("업스테이지",), value),), registry,
        identity_provider=AllRelated(), grouping_provider=provider)
    assert provider.calls == 2
    assert len(events) == 1
    assert events[0].primary.article is value
    assert metrics["grouping_failures"] == 1


def test_model_grouping_replaces_blog_representative_when_normal_article_is_available() -> None:
    registry = load_portfolio_registry("config/portfolio_registry.yaml")
    blog = article("업스테이지 독파모 2차 통과 : 네이버 블로그 - Naver Blog")
    report = article("업스테이지 독파모 2차 통과 - 연합뉴스")

    class BlogFirstGrouping:
        def partition(self, *, candidates, **kwargs):
            return (EventGroup(tuple(item.article_id for item in candidates), candidates[0].article_id, "독파모 2차 통과"),)

    events, _ = prepare_events((RouteAMatch("업스테이지", ("업스테이지",), blog),
        RouteAMatch("업스테이지", ("업스테이지",), report)), registry,
        identity_provider=AllRelated(), grouping_provider=BlogFirstGrouping())
    assert events[0].primary.article is report


def test_bulk_grouping_fallback_merges_chunk_provisionals_without_article_loss() -> None:
    registry = load_portfolio_registry("config/portfolio_registry.yaml")
    values = [article("업스테이지 독파모 평가 결과"), article("업스테이지 독파모 다음 단계 진출"),
        article("업스테이지 NVIDIA B200 지원"), article("업스테이지 KT 모두의 AI 컨소시엄")]

    class BulkOnlyGrouping:
        calls = 0
        def partition(self, *, candidates, **kwargs):
            self.calls += 1
            if len(candidates) == 4 and self.calls <= 2:
                return ()
            if all(item.article_id.startswith("provisional-") for item in candidates):
                return (
                    EventGroup((candidates[0].article_id, candidates[1].article_id), candidates[0].article_id, "독파모 평가 결과"),
                    EventGroup((candidates[2].article_id,), candidates[2].article_id, "NVIDIA B200"),
                    EventGroup((candidates[3].article_id,), candidates[3].article_id, "KT 모두의 AI"),
                )
            return tuple(EventGroup((item.article_id,), item.article_id, item.title) for item in candidates)

    events, metrics = prepare_events(tuple(RouteAMatch("업스테이지", ("업스테이지",), value) for value in values), registry,
        identity_provider=AllRelated(), grouping_provider=BulkOnlyGrouping())
    assert [event.coverage_count for event in events] == [2, 1, 1]
    assert sum(event.coverage_count for event in events) == len(values)
    assert metrics["grouping_failures"] == 1


def test_model_first_filters_lexical_noise_and_uses_group_label_for_event_identity() -> None:
    registry = load_portfolio_registry("config/portfolio_registry.yaml")
    related, noise = article("업스테이지, 독파모 2차 평가 통과"), article("식중독균이 빠르게 자란다")
    events, metrics = prepare_events((RouteAMatch("업스테이지", ("업스테이지",), related),
        RouteAMatch("업스테이지", ("업스테이지",), noise)), registry, identity_provider=Identity(), grouping_provider=Grouping())
    assert metrics["identity_related"] == 1 and metrics["identity_not_related"] == 1
    assert {row["verdict"] for row in metrics["identity_decisions"]} == {"RELATED", "NOT_RELATED"}
    assert metrics["company_stage_counts"][0]["identity_related"] == 1
    assert len(events) == 1
    assert {article_id(match.article) for match in events[0].all_matches} == {article_id(related)}
    assert events[0].semantic_fingerprint


def test_group_partition_rejects_missing_duplicate_or_invented_ids() -> None:
    values = [EventCandidate("a", "a", "", "x", NOW), EventCandidate("b", "b", "", "x", NOW)]
    with pytest.raises(ValueError):
        validate_partition(values, (EventGroup(("a", "a"), "a", "event"),))
    with pytest.raises(ValueError):
        validate_partition(values, (EventGroup(("a", "invented"), "a", "event"),))
