from __future__ import annotations

import pandas as pd

from tv_history.chart import AssetChartService, ChartResult, parse_days
from tv_history.storage import CsvStorage

from test_analysis import FakeSynchronizer, hourly_frame, settings
from test_bars import session_frame


def test_parse_days_accepts_integer_and_numeric_string() -> None:
    assert parse_days(10) == 10
    assert parse_days("10") == 10


def test_parse_days_rejects_non_integer_values() -> None:
    for value in (True, 0, 366, "10.5", "abc"):
        try:
            parse_days(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected {value!r} to be rejected")


def test_chart_returns_png_and_metadata(tmp_path) -> None:
    configured = settings(tmp_path)
    sync = FakeSynchronizer(hourly_frame())
    service = AssetChartService(configured, sync, CsvStorage(configured))

    result = service.render("BTCUSD:BITSTAMP", "4h", "2026-01-11T09:30:00Z", "10")

    assert isinstance(result, ChartResult)
    assert result.image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert result.metadata["requested_days"] == 10
    assert result.metadata["effective_bar_close"] == "2026-01-11T08:00:00+00:00"
    assert result.metadata["bar_open"] == "2026-01-11T04:00:00Z"
    assert result.metadata["bar_close"] == "2026-01-11T08:00:00Z"
    assert result.metadata["is_bar_complete"] is True
    for removed in (
        "requested_from", "bars_rendered", "coverage_complete", "source", "refresh",
        "reference_prices", "price_decimals",
    ):
        assert removed not in result.metadata


def test_execution_chart_returns_completed_visible_price_references(tmp_path) -> None:
    configured = settings(tmp_path)
    frame = hourly_frame()
    sync = FakeSynchronizer(frame)
    service = AssetChartService(configured, sync, CsvStorage(configured))

    result = service.render(
        "BTCUSD:BITSTAMP", "4h", "2026-01-11T09:30:00Z", "10", "execution"
    )

    assert isinstance(result, ChartResult)
    assert result.image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert result.metadata["reference_prices"] == {
        "last_close": 124.7,
        "chart_low": 100.6,
        "chart_high": 124.9,
    }
    assert result.metadata["price_decimals"] == 2
    assert result.metadata["effective_bar_close"] == "2026-01-11T08:00:00+00:00"


def test_chart_accepts_tradingview_exchange_symbol_format(tmp_path) -> None:
    configured = settings(tmp_path)
    sync = FakeSynchronizer(hourly_frame())
    service = AssetChartService(configured, sync, CsvStorage(configured))

    result = service.render("RUS:MX1!", "4h", "2026-01-11T09:30:00Z", "10")

    assert isinstance(result, ChartResult)
    assert result.metadata["asset"] == "RUS:MX1!"
    assert sync.requested_timeframes == ["4h"]


def test_chart_sessions_walk_back_across_weekend_and_report_coverage(tmp_path) -> None:
    configured = settings(tmp_path)
    service = AssetChartService(
        configured, FakeSynchronizer(session_frame()), CsvStorage(configured)
    )

    calendar = service.render(
        "ICEEUR:BRN1!", "1h", "2026-03-23T00:00:00Z", 4
    )
    sessions = service.render(
        "ICEEUR:BRN1!", "1h", "2026-03-23T00:00:00Z", 4, sessions=4
    )

    assert isinstance(calendar, ChartResult)
    assert isinstance(sessions, ChartResult)
    assert calendar.metadata["sessions_covered"] == 2
    assert calendar.metadata["requested_days"] == 4
    assert sessions.metadata["sessions_covered"] == 4
    assert sessions.metadata["requested_sessions"] == 4


def test_chart_no_bars_returns_structured_error_for_both_versions(tmp_path) -> None:
    configured = settings(tmp_path)
    service = AssetChartService(
        configured, FakeSynchronizer(session_frame()), CsvStorage(configured)
    )

    legacy = service.render("ICEEUR:BRN1!", "1h", "2020-01-01T00:00:00Z", 4)
    execution = service.render(
        "ICEEUR:BRN1!", "1h", "2020-01-01T00:00:00Z", 4, "execution"
    )

    assert legacy == execution
    assert legacy["error"]["code"] == "NO_BARS_IN_WINDOW"
    assert legacy["error"]["retryable"] is False


def test_continuous_asset_counts_weekend_dates_as_sessions(tmp_path) -> None:
    configured = settings(tmp_path)
    index = pd.date_range(
        "2026-03-19T00:00:00Z", periods=4 * 24, freq="1h", name="timestamp_utc"
    )
    closes = pd.Series(range(len(index)), index=index, dtype=float) + 100
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": closes + 1,
            "low": closes - 1,
            "close": closes + 0.5,
            "volume": 1000.0,
        },
        index=index,
    )
    service = AssetChartService(
        configured, FakeSynchronizer(frame), CsvStorage(configured)
    )

    calendar = service.render(
        "BITSTAMP:BTCUSD", "1h", "2026-03-23T00:00:00Z", 4
    )
    sessions = service.render(
        "BITSTAMP:BTCUSD", "1h", "2026-03-23T00:00:00Z", 4, sessions=4
    )

    assert calendar.metadata["sessions_covered"] == 4
    assert sessions.metadata["sessions_covered"] == 4


def test_invalid_days_does_not_synchronize(tmp_path) -> None:
    configured = settings(tmp_path)
    sync = FakeSynchronizer(hourly_frame())
    service = AssetChartService(configured, sync, CsvStorage(configured))

    result = service.render("BTCUSD:BITSTAMP", "4h", None, "nope")

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert sync.calls == 0


def test_excessive_bar_count_returns_error(tmp_path) -> None:
    configured = settings(tmp_path)
    index = pd.date_range("2026-01-01", periods=2000, freq="1h", tz="UTC", name="timestamp_utc")
    closes = pd.Series(range(2000), index=index, dtype=float) + 100
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": closes + 1,
            "low": closes - 1,
            "close": closes + 0.5,
            "volume": 1000.0,
        },
        index=index,
    )
    sync = FakeSynchronizer(frame)
    service = AssetChartService(configured, sync, CsvStorage(configured))

    result = service.render("BTCUSD:BITSTAMP", "1h", "2026-03-25T08:00:00Z", 84)

    assert result["error"]["code"] == "TOO_MANY_BARS_TO_RENDER"
