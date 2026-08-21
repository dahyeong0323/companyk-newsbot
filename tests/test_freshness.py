from __future__ import annotations

from datetime import UTC, datetime, timedelta

from companyk_newsbot.freshness import delivery_window, filter_articles, full_shadow_window, rss_freshness_hint, smoke_window
from companyk_newsbot.models import Article


NOW = datetime(2026, 8, 12, 5, 0, tzinfo=UTC)


def article(published_at: datetime | None) -> Article:
    return Article(
        source="fixture",
        source_type="fixture",
        title="Fixture news",
        url="https://example.com/news",
        canonical_url="https://example.com/news",
        published_at=published_at,
        retrieved_at=NOW,
    )


def test_delivery_window_uses_last_successful_run_with_overlap_not_calendar_date() -> None:
    checkpoint = datetime(2026, 8, 11, 22, 5, tzinfo=UTC)
    window = delivery_window(now=NOW, last_successful_delivery_run=checkpoint, overlap_hours=2, first_run_hours=30)
    boundary = checkpoint - timedelta(hours=2)
    result = filter_articles(
        [article(boundary), article(boundary - timedelta(microseconds=1)), article(NOW)],
        window=window,
    )

    assert window.start == boundary
    assert len(result.accepted) == 2
    assert result.rejected_too_old == 1


def test_first_run_fallback_is_bounded_to_30_hours() -> None:
    window = delivery_window(now=NOW, last_successful_delivery_run=None, overlap_hours=2, first_run_hours=30)
    result = filter_articles(
        [article(NOW - timedelta(hours=30)), article(NOW - timedelta(hours=30, microseconds=1))],
        window=window,
    )

    assert window.start == NOW - timedelta(hours=30)
    assert len(result.accepted) == 1
    assert result.rejected_too_old == 1


def test_full_shadow_window_uses_an_explicit_hourly_lookback() -> None:
    window = full_shadow_window(now=NOW, lookback_hours=30)

    assert window.mode == "full_shadow_lookback"
    assert window.start == NOW - timedelta(hours=30)
    assert window.end == NOW


def test_future_and_missing_timestamps_are_rejected_separately() -> None:
    window = smoke_window(now=NOW, lookback_days=7)
    result = filter_articles([article(NOW + timedelta(seconds=1)), article(None)], window=window)

    assert result.accepted == ()
    assert result.rejected_future == 1
    assert result.rejected_missing_timestamp == 1


def test_smoke_accepts_full_seven_day_window_across_kst_dates() -> None:
    window = smoke_window(now=NOW, lookback_days=7)
    result = filter_articles([article(NOW - timedelta(days=6, hours=23))], window=window)

    assert window.mode == "smoke_7d"
    assert len(result.accepted) == 1


def test_rss_hint_expands_after_a_delivery_outage_without_exceeding_cap() -> None:
    window = delivery_window(
        now=NOW,
        last_successful_delivery_run=NOW - timedelta(days=4),
        overlap_hours=2,
        first_run_hours=30,
    )

    assert rss_freshness_hint(window) == "when:5d"
    assert rss_freshness_hint(window, maximum_days=3) == "when:3d"
