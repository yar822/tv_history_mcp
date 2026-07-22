import pandas as pd

from tv_history.resample import bar_status, completed_as_of, resample_ohlcv


def test_simple_four_hour_resample_and_closed_bar_filter() -> None:
    index = pd.date_range("2026-01-01", periods=8, freq="1h", tz="UTC", name="timestamp_utc")
    hourly = pd.DataFrame(
        {
            "open": range(8),
            "high": range(1, 9),
            "low": range(8),
            "close": range(1, 9),
            "volume": [10] * 8,
        },
        index=index,
    )

    four_hour = resample_ohlcv(hourly, "4h")
    closed = completed_as_of(four_hour, "4h", pd.Timestamp("2026-01-01T05:00Z"))

    assert len(four_hour) == 2
    assert len(closed) == 1
    assert closed.iloc[0].to_dict() == {
        "open": 0,
        "high": 4,
        "low": 0,
        "close": 4,
        "volume": 40,
    }


def test_bar_status_uses_supplied_resampled_row() -> None:
    bar_open = pd.Timestamp("2026-07-17T20:00:00Z")
    inside = bar_status("4h", bar_open, pd.Timestamp("2026-07-17T21:00:00Z"))
    assert inside == {
        "bar_open": "2026-07-17T20:00:00Z",
        "bar_close": "2026-07-18T00:00:00Z",
        "is_bar_complete": False,
    }

    at_close = bar_status("4h", bar_open, pd.Timestamp("2026-07-18T00:00:00Z"))
    assert at_close == {
        "bar_open": "2026-07-17T20:00:00Z",
        "bar_close": "2026-07-18T00:00:00Z",
        "is_bar_complete": True,
    }
