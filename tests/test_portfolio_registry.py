from pathlib import Path

import pytest

from companyk_newsbot.portfolio_registry import build_direct_query_plan, display_name, load_portfolio_registry, parse_source_name


REGISTRY = Path("config/portfolio_registry.yaml")


def test_current_registry_loads_with_full_workbook_coverage() -> None:
    registry = load_portfolio_registry(REGISTRY)
    plan = build_direct_query_plan(registry)
    assert registry.source.company_count == len(registry.companies) == 155
    assert registry.source.source_sha256 == "e2386c135112c8e505fc2bcd8e98f4994ea567fc9d51e7bda1a0a26ee53282a5"
    assert registry.source.sheet == "Sheet2" and registry.source.column == "A"
    assert len(plan.queries) == 164
    assert plan.uncovered_company_ids == ()
    assert all(company.search_terms and company.match_terms for company in registry.companies)
    assert len({company.source_name for company in registry.companies}) == 155


def test_former_name_parser_preserves_english_commas_and_korean_variants() -> None:
    assert parse_source_name("Breeze Bio, Inc.(구, GenEdit, Inc.)") == ("Breeze Bio, Inc.", "GenEdit, Inc.")
    assert parse_source_name("(주)시나몬(구.봉봉)") == ("(주)시나몬", "봉봉")
    assert display_name("Noah's Farm Pte.Ltd.") == "Noah's Farm"
    assert display_name("MPN Marketplace Networks Gmbh") == "MPN Marketplace Networks"


def test_registry_does_not_depend_on_legacy_route_b_file(monkeypatch) -> None:
    import companyk_newsbot.config as legacy
    monkeypatch.setattr(legacy, "load_keyword_map", lambda *args, **kwargs: pytest.fail("legacy Route B config loaded"))
    assert load_portfolio_registry(REGISTRY).companies
