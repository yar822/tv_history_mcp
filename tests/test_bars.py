from __future__ import annotations

import pandas as pd

from tv_history.bars import AssetBarsService
from tv_history.analysis import short_term_metrics
from tv_history.mcp_response import mcp_result

from test_analysis import FakeSynchronizer, settings


def session_frame() -> pd.DataFrame:
    timestamps = []
    for day in (17, 18, 19, 20):
        timestamps.extend(
            pd.date_range(
                f"2026-03-{day:02d}T01:00:00Z",
                periods=12,
                freq="1h",
            )
        )
    timestamps.extend(
        [pd.Timestamp("2026-03-22T22:00:00Z"), pd.Timestamp("2026-03-22T23:00:00Z")]
    )
    timestamps.append(pd.Timestamp("2026-03-23T00:00:00Z"))
    index = pd.DatetimeIndex(timestamps, name="timestamp_utc")
    closes = pd.Series(
        [110 + ((position % 12) - 6) * (-1 if position // 12 % 2 else 1)
         for position in range(len(index))],
        index=index,
        dtype=float,
    )
    return pd.DataFrame(
        {
            "open": closes - 0.2,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": 1000.0,
        },
        index=index,
    )


def test_asset_bars_sessions_are_completed_oldest_first(tmp_path) -> None:
    service = AssetBarsService(settings(tmp_path), FakeSynchronizer(session_frame()))

    result = service.get_bars(
        "ICEEUR:BRN1!", "1h", "2026-03-23T00:00:00Z", sessions=4
    )

    assert result["sessions_covered"] == 4
    assert result["bars_returned"] == 38
    assert result["effective_bar_close"] == "2026-03-23T00:00:00+00:00"
    assert [bar["t"][:10] for bar in result["bars"]][0] == "2026-03-18"
    assert [bar["t"][:10] for bar in result["bars"]][-1] == "2026-03-22"
    assert [bar["t"] for bar in result["bars"]] == sorted(
        bar["t"] for bar in result["bars"]
    )
    assert result["atr_14"] is not None


def test_asset_bars_count_returns_exact_completed_tail(tmp_path) -> None:
    service = AssetBarsService(settings(tmp_path), FakeSynchronizer(session_frame()))

    result = service.get_bars(
        "ICEEUR:BRN1!", "1h", "2026-03-23T00:00:00Z", count=48
    )

    assert result["bars_returned"] == 48
    assert result["bars"][-1]["t"] == "2026-03-22T23:00:00Z"
    assert all(pd.Timestamp(bar["t"]) + pd.Timedelta(hours=1) <= pd.Timestamp("2026-03-23T00:00:00Z")
               for bar in result["bars"])


def test_asset_bars_sessions_override_count_and_short_history_is_not_error(tmp_path) -> None:
    service = AssetBarsService(settings(tmp_path), FakeSynchronizer(session_frame()))

    sessions_result = service.get_bars(
        "ICEEUR:BRN1!", "1h", "2026-03-23T00:00:00Z", count=5000, sessions=2
    )
    short_result = service.get_bars(
        "ICEEUR:BRN1!", "1h", "2026-03-18T02:00:00Z", count=1000
    )

    assert sessions_result["bars_returned"] == 14
    assert sessions_result["sessions_covered"] == 2
    assert "error" not in short_result
    assert short_result["bars_returned"] < 1000


def test_count_path_reports_sunday_reopen_as_a_second_session(tmp_path) -> None:
    service = AssetBarsService(settings(tmp_path), FakeSynchronizer(session_frame()))

    result = service.get_bars(
        "ICEEUR:BRN1!", "1h", "2026-03-23T00:00:00Z", count=8
    )

    assert result["bars_returned"] == 8
    assert result["sessions_covered"] == 2
    assert result["bars"][-1]["t"] == "2026-03-22T23:00:00Z"


def test_short_term_window_includes_sunday_reopen() -> None:
    completed = session_frame().iloc[:-1]

    result = short_term_metrics(completed, 4, row_atr=2.0)

    assert result["sessions_covered"] == 4
    assert result["bars_used"] == 38


def test_mcp_error_result_sets_protocol_error_without_an_image() -> None:
    result = mcp_result(
        {"error": {"code": "NO_BARS_IN_WINDOW", "message": "none", "retryable": False}}
    )

    assert result.isError is True
    assert all(content.type != "image" for content in result.content)


def test_asset_bars_rejects_count_above_cap_when_sessions_are_absent(tmp_path) -> None:
    service = AssetBarsService(settings(tmp_path), FakeSynchronizer(session_frame()))

    result = service.get_bars(
        "ICEEUR:BRN1!", "1h", "2026-03-23T00:00:00Z", count=1001
    )

    assert result["error"]["code"] == "INVALID_PARAMETER"
