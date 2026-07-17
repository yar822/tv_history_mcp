from __future__ import annotations

import pandas as pd

from tv_history.chart import AssetChartService, ChartResult, parse_days
from tv_history.storage import CsvStorage

from test_analysis import FakeSynchronizer, hourly_frame, settings


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
    assert result.metadata["bars_rendered"] == 60
    assert result.metadata["requested_days"] == 10


def test_invalid_days_does_not_synchronize(tmp_path) -> None:
    configured = settings(tmp_path)
    sync = FakeSynchronizer(hourly_frame())
    service = AssetChartService(configured, sync, CsvStorage(configured))

    result = service.render("BTCUSD:BITSTAMP", "4h", None, "nope")

    assert result["error"] == "invalid_parameter"
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

    assert result["error"] == "too_many_bars_to_render"
