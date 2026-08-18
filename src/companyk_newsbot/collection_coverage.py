"""Explicit product decision separating a valid empty day from an RSS outage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from companyk_newsbot.collectors.google_news_rss import RSSCollectionResult


CoverageStatus = Literal["SUFFICIENT", "INCONCLUSIVE"]
DEFAULT_RSS_MIN_SUCCESS_RATIO = 0.90
DEFAULT_ZERO_NEWS_MIN_SUCCESS_RATIO = 0.98


@dataclass(frozen=True)
class CollectionCoverageAssessment:
    status: CoverageStatus
    query_total: int
    query_successes: int
    query_failures: int
    success_ratio: float
    threshold: float
    reason: str | None = None

    @property
    def sufficient(self) -> bool:
        return self.status == "SUFFICIENT"

    def metrics(self) -> dict[str, int | float | str]:
        return {
            "collection_coverage_status": self.status,
            "collection_coverage_threshold": self.threshold,
            "rss_query_total": self.query_total,
            "rss_query_success": self.query_successes,
            "rss_query_failure": self.query_failures,
            "rss_success_ratio": self.success_ratio,
        }


def assess_collection_coverage(
    result: RSSCollectionResult,
    *,
    threshold: float = DEFAULT_RSS_MIN_SUCCESS_RATIO,
) -> CollectionCoverageAssessment:
    if not 0 <= threshold <= 1:
        raise ValueError("RSS minimum success ratio must be between 0 and 1")
    total = len(result.queries)
    if total == 0:
        raise ValueError("RSS collection requires at least one configured direct query")
    successes = len(result.successes)
    ratio = successes / total
    sufficient = ratio >= threshold
    return CollectionCoverageAssessment(
        status="SUFFICIENT" if sufficient else "INCONCLUSIVE",
        query_total=total,
        query_successes=successes,
        query_failures=total - successes,
        success_ratio=ratio,
        threshold=threshold,
        reason=None if sufficient else "collection_coverage_below_threshold",
    )


def assess_zero_news_health(
    result: RSSCollectionResult,
    *,
    threshold: float = DEFAULT_ZERO_NEWS_MIN_SUCCESS_RATIO,
) -> CollectionCoverageAssessment:
    """Apply a stricter request-success gate before declaring a raw zero-news day."""
    assessment = assess_collection_coverage(result, threshold=threshold)
    if assessment.sufficient:
        return assessment
    return CollectionCoverageAssessment(
        **{**assessment.__dict__, "reason": "zero_news_collection_health_below_threshold"}
    )
