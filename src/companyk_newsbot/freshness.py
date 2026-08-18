"""Runtime freshness windows independent from portfolio business rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Iterable, Literal

from companyk_newsbot.models import Article


FreshnessMode = Literal["smoke_7d", "since_last_successful_run"]


@dataclass(frozen=True)
class FreshnessWindow:
    mode: FreshnessMode
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("freshness window datetimes must be timezone-aware")
        if self.start > self.end:
            raise ValueError("freshness window start must not exceed end")


@dataclass(frozen=True)
class FreshnessResult:
    window: FreshnessWindow
    accepted: tuple[Article, ...]
    rejected_too_old: int
    rejected_future: int
    rejected_missing_timestamp: int


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def smoke_window(*, now: datetime, lookback_days: int) -> FreshnessWindow:
    if lookback_days < 1:
        raise ValueError("smoke lookback days must be positive")
    end = utc_datetime(now)
    return FreshnessWindow("smoke_7d", end - timedelta(days=lookback_days), end)


def delivery_window(
    *,
    now: datetime,
    last_successful_delivery_run: datetime | None,
    overlap_hours: int,
    first_run_hours: int,
) -> FreshnessWindow:
    if overlap_hours < 0 or first_run_hours < 1:
        raise ValueError("freshness overlap must be non-negative and first-run hours must be positive")
    end = utc_datetime(now)
    if last_successful_delivery_run is None:
        start = end - timedelta(hours=first_run_hours)
    else:
        start = utc_datetime(last_successful_delivery_run) - timedelta(hours=overlap_hours)
    return FreshnessWindow("since_last_successful_run", start, end)


def rss_freshness_hint(window: FreshnessWindow, *, maximum_days: int = 7) -> str:
    """Return a Google News `when:` hint that covers the full delivery window.

    The local timestamp filter remains authoritative.  This hint only prevents
    Google News from silently excluding the beginning of a long outage gap.
    """
    if maximum_days < 1:
        raise ValueError("maximum RSS lookback days must be positive")
    seconds = max(0.0, (window.end - window.start).total_seconds())
    days = max(1, ceil(seconds / timedelta(days=1).total_seconds()))
    return f"when:{min(days, maximum_days)}d"


def filter_articles(articles: Iterable[Article], *, window: FreshnessWindow) -> FreshnessResult:
    accepted: list[Article] = []
    too_old = future = missing = 0
    for article in articles:
        if article.published_at is None:
            missing += 1
            continue
        published_at = utc_datetime(article.published_at)
        if published_at < window.start:
            too_old += 1
        elif published_at > window.end:
            future += 1
        else:
            accepted.append(article)
    return FreshnessResult(window, tuple(accepted), too_old, future, missing)
