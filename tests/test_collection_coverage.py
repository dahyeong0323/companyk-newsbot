from __future__ import annotations

import pytest

from companyk_newsbot.collection_coverage import assess_collection_coverage, assess_zero_news_health
from companyk_newsbot.collectors.google_news_rss import QueryCollectionResult, RSSCollectionResult


def collection(successes: int, total: int) -> RSSCollectionResult:
    return RSSCollectionResult(tuple(
        QueryCollectionResult(f"q{index}", "success" if index < successes else "http_error")
        for index in range(total)
    ))


@pytest.mark.parametrize(
    "successes, expected",
    [(164, "SUFFICIENT"), (160, "SUFFICIENT"), (148, "SUFFICIENT"),
     (147, "INCONCLUSIVE"), (50, "INCONCLUSIVE"), (0, "INCONCLUSIVE")],
)
def test_default_coverage_threshold_boundary(successes: int, expected: str) -> None:
    assessment = assess_collection_coverage(collection(successes, 164))
    assert assessment.status == expected
    assert assessment.success_ratio == successes / 164
    assert assessment.threshold == 0.90


def test_zero_total_queries_is_configuration_error() -> None:
    with pytest.raises(ValueError, match="at least one configured direct query"):
        assess_collection_coverage(RSSCollectionResult(()))


def test_zero_news_health_requires_stricter_coverage_than_normal_briefing() -> None:
    assert assess_collection_coverage(collection(160, 164)).sufficient is True
    zero_news = assess_zero_news_health(collection(160, 164))

    assert zero_news.sufficient is False
    assert zero_news.reason == "zero_news_collection_health_below_threshold"
    assert zero_news.threshold == 0.98


@pytest.mark.parametrize("threshold", [-0.01, 1.01])
def test_invalid_threshold_is_rejected(threshold: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        assess_collection_coverage(collection(1, 1), threshold=threshold)
