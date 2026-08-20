from __future__ import annotations

import pytest

from companyk_newsbot.config import load_keyword_map
from companyk_newsbot.e2e import E2EExecutionError, PRODUCTION_RECIPIENTS, TEST_RECIPIENTS, _assert_production_recipient, _assert_test_recipient, build_query_plan
from companyk_newsbot.email import ResendSettings
from companyk_newsbot.rules import ExposureRegistry


def test_smoke_caps_and_full_shadow_coverage_are_separate() -> None:
    config = load_keyword_map()
    registry = ExposureRegistry(config)

    smoke = build_query_plan(config, profile="smoke", direct_cap=8, exposure_cap=8)
    full = build_query_plan(config, profile="full_shadow")

    assert len(smoke.direct_queries) == 8
    assert len(smoke.exposure_queries) == 8
    assert set(smoke.direct_queries).issubset(full.direct_queries)
    assert set(smoke.exposure_queries).issubset(full.exposure_queries)
    assert len(full.direct_queries) == len(config.company_rules) == 46
    assert len(full.exposure_queries) == len(registry.queries)


def test_smoke_sampling_is_deterministic_and_not_config_prefix() -> None:
    config = load_keyword_map()

    first = build_query_plan(config, profile="smoke", direct_cap=8, exposure_cap=8)
    second = build_query_plan(config, profile="smoke", direct_cap=8, exposure_cap=8)

    assert first == second
    assert first.direct_queries != tuple(config.company_rules)[:8]


def test_query_plan_fetches_cross_route_normalized_query_once() -> None:
    config = load_keyword_map()
    registry = ExposureRegistry(config)
    shared_query = registry.queries[0].query
    original_company_name = next(iter(config.company_rules))
    rule = config.company_rules.pop(original_company_name)
    config.company_rules[shared_query.swapcase()] = rule

    plan = build_query_plan(config, profile="full_shadow")

    normalized = " ".join(shared_query.casefold().split())
    assert sum(" ".join(query.casefold().split()) == normalized for query in plan.queries) == 1
    assert len(registry.lookup(shared_query).links) >= 1


def test_smoke_delivery_guard_accepts_only_fixed_test_recipient() -> None:
    _assert_test_recipient(ResendSettings("secret", (TEST_RECIPIENTS[0].upper(),), "Bot <bot@example.com>"))
    with pytest.raises(E2EExecutionError, match="may send only"):
        _assert_test_recipient(ResendSettings("secret", ("production@example.com",), "Bot <bot@example.com>"))


def test_production_delivery_guard_accepts_only_the_approved_recipient_list() -> None:
    _assert_production_recipient(ResendSettings("secret", PRODUCTION_RECIPIENTS, "Bot <bot@example.com>"))
    with pytest.raises(E2EExecutionError, match="recipients must match"):
        _assert_production_recipient(ResendSettings("secret", (PRODUCTION_RECIPIENTS[0],), "Bot <bot@example.com>"))
