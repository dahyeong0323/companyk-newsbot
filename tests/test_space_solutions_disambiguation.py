from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companyk_newsbot.models import Article
from companyk_newsbot.portfolio_registry import build_direct_query_plan, load_portfolio_registry
from companyk_newsbot.rules import RouteADetector


REGISTRY_PATH = "config/portfolio_registry.yaml"


def article(title: str, description: str | None = None) -> Article:
    return Article(
        source="fixture",
        source_type="fixture",
        title=title,
        description=description,
        url="https://example.com/space-solutions",
        canonical_url="https://example.com/space-solutions",
        retrieved_at=datetime(2026, 8, 14, tzinfo=UTC),
        origin_metadata={"query": "스페이스솔루션", "origin_queries": ["스페이스솔루션"]},
    )


def detector() -> RouteADetector:
    return RouteADetector(load_portfolio_registry(REGISTRY_PATH))


@pytest.mark.parametrize(
    "title, description",
    [
        ("스페이스솔루션, 항공우주 부품 생산 확대", None),
        ("주식회사 스페이스솔루션 로켓용 고압 제어용밸브 공급", None),
        ("스페이스솔루션 신규 시설 투자", "대전 유성구에서 생산 시설을 운영한다."),
        ("스페이스솔루션이 신규 계약을 체결했다", "자세한 내용은 spacesolutions.co.kr에서 확인할 수 있다."),
    ],
)
def test_target_legal_entity_context_matches(title: str, description: str | None) -> None:
    matches = detector().detect_scoped(article(title, description))
    assert [match.company for match in matches] == ["스페이스솔루션"]


@pytest.mark.parametrize("wrong_context", ["PIM", "코넥스", "KONEX"])
def test_known_wrong_same_name_company_context_is_rejected(wrong_context: str) -> None:
    # Include a positive location word to prove explicit wrong-entity context wins.
    value = article(f"스페이스솔루션 {wrong_context} 사업 확대", "대전 관련 시장도 검토한다.")
    assert detector().detect_scoped(value) == []


def test_bare_ambiguous_name_fails_closed() -> None:
    assert detector().detect_scoped(article("스페이스솔루션이 신제품을 공개했다")) == []


@pytest.mark.parametrize(
    "title",
    [
        "우주 공간 솔루션 기술이 주목받는다",
        "Space Solution expands its KONEX PIM offering",
        "스페이스솔루션즈가 신규 서비스를 공개했다",
    ],
)
def test_unregistered_fragments_and_english_variant_do_not_match(title: str) -> None:
    assert detector().detect_scoped(article(title)) == []


def test_registry_anchors_exact_non_personal_legal_entity_metadata() -> None:
    registry = load_portfolio_registry(REGISTRY_PATH)
    company = next(value for value in registry.companies if value.display_name == "스페이스솔루션")
    metadata = company.identity_metadata

    assert company.search_terms == ["스페이스솔루션"]
    assert "주식회사 스페이스솔루션" in company.legal_names
    assert company.ambiguity.negative_context == ["PIM", "코넥스", "KONEX"]
    assert metadata is not None
    assert metadata.corporate_registry_number == "135011-0105756"
    assert metadata.registered_head_office == "대전광역시 유성구 문지로 229(문지동)"
    assert metadata.website == "https://www.spacesolutions.co.kr"
    assert "항공우주(로켓)분야 고압 제어용밸브 제조 및 판매업" in metadata.business_purposes
    assert "누리호" not in " ".join((*company.ambiguity.required_context, *metadata.business_purposes))
    assert len(build_direct_query_plan(registry).queries) == 164
