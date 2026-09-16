from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from tv_history.bars import (
    AssetBarsService,
    finality_flags,
    finalization_delay,
)
from tv_history.analysis import short_term_metrics
from tv_history.mcp_response import mcp_result

from test_analysis import FakeSynchronizer, settings


def test_small_response_checks_only_returned_rows_with_full_successor_evidence(tmp_path, monkeypatch):
    import tv_history.bars as module
    from test_analysis import hourly_frame
    source = hourly_frame()
    successor = source.index[-1] + pd.Timedelta(hours=1)
    source.attrs["completion_context"] = {
        "timeframe": "1h", "raw_opens": [t.isoformat() for t in source.index] + [successor.isoformat()]}
    original = module.completion_details
    expected = original(source, pd.Timedelta(hours=1), successor, pd.Timedelta(minutes=5)).tail(3)
    sizes = []
    def tracked(opened, *args):
        sizes.append(len(opened))
        assert len(opened.attrs["completion_context"]["raw_opens"]) == len(source) + 1
        return original(opened, *args)
    monkeypatch.setattr(module, "completion_details", tracked)
    result = AssetBarsService(settings(tmp_path), FakeSynchronizer(source)).get_bars(
        "BITSTAMP:BTCUSD", "1h", successor.isoformat(), count=3)
    assert sizes == [3]
    assert [{k: bar[k] for k in expected.columns} for bar in result["bars"]] == expected.to_dict("records")
    assert result["bars"][-1]["is_bar_complete"] is True
    assert "completion_context" in source.attrs


@pytest.mark.parametrize("fetched,cutoff,complete,reason", [
    (None, "2026-09-01T16:06Z", False, "awaiting_fresh_data"),
    ("2026-09-01T16:04Z", "2026-09-01T16:06Z", False, "awaiting_fresh_data"),
    ("2026-09-01T16:05Z", "2026-09-01T16:06Z", True, "session_boundary_delay"),
    ("2026-09-01T16:05Z", "2026-09-01T16:04Z", False, "awaiting_boundary_delay"),
])
def test_selected_bar_retains_boundary_and_receipt_evidence(tmp_path, fetched, cutoff, complete, reason):
    from test_calendar_finality import ASSET, calendar, configured, with_evidence
    source = with_evidence(calendar(tmp_path), "1h", "2026-09-01T15:00Z", fetched)
    cfg = replace(configured(tmp_path), indicators=settings(tmp_path).indicators)
    result = AssetBarsService(cfg, FakeSynchronizer(source)).get_bars(
        ASSET, "1h", cutoff, count=1)
    assert result["bars"][0]["is_bar_complete"] is complete
    assert result["bars"][0]["completion_reason"] == reason
    assert pd.Timestamp(result["bars"][0]["completion_boundary"]) == pd.Timestamp("2026-09-01T16:00Z")


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


def test_asset_bars_sessions_include_latest_opened_bar_oldest_first(tmp_path) -> None:
    configured = replace(
        settings(tmp_path), finalization_delay_by_exchange={"ICEEUR": 25}
    )
    service = AssetBarsService(configured, FakeSynchronizer(session_frame()))

    result = service.get_bars(
        "ICEEUR:BRN1!", "1h", "2026-03-23T00:00:00Z", sessions=4
    )

    assert result["sessions_covered"] == 4
    assert result["bars_returned"] == 27
    assert result["effective_bar_close"] == "2026-03-23T01:00:00+00:00"
    assert [bar["t"][:10] for bar in result["bars"]][0] == "2026-03-19"
    assert [bar["t"][:10] for bar in result["bars"]][-1] == "2026-03-23"
    assert [bar["t"] for bar in result["bars"]] == sorted(
        bar["t"] for bar in result["bars"]
    )
    assert result["atr_14"] is not None
    assert result["bars"][-1]["is_bar_complete"] is False
    assert result["bars"][-2]["is_bar_complete"] is True
    assert set(result["bars"][-1]) == {
        "t", "is_bar_complete", "completion_reason", "completion_boundary", "o", "h", "l", "c", "v"
    }


def test_asset_bars_count_returns_exact_completed_tail(tmp_path) -> None:
    service = AssetBarsService(settings(tmp_path), FakeSynchronizer(session_frame()))

    result = service.get_bars(
        "ICEEUR:BRN1!", "1h", "2026-03-23T00:00:00Z", count=48
    )

    assert result["bars_returned"] == 48
    assert result["bars"][-1]["t"] == "2026-03-23T00:00:00Z"
    assert all(pd.Timestamp(bar["t"]) <= pd.Timestamp("2026-03-23T00:00:00Z")
               for bar in result["bars"])


def test_asset_bars_sessions_override_count_and_short_history_is_not_error(tmp_path) -> None:
    service = AssetBarsService(settings(tmp_path), FakeSynchronizer(session_frame()))

    sessions_result = service.get_bars(
        "ICEEUR:BRN1!", "1h", "2026-03-23T00:00:00Z", count=5000, sessions=2
    )
    short_result = service.get_bars(
        "ICEEUR:BRN1!", "1h", "2026-03-18T02:00:00Z", count=1000
    )

    assert sessions_result["bars_returned"] == 3
    assert sessions_result["sessions_covered"] == 2
    assert "error" not in short_result
    assert short_result["bars_returned"] < 1000


def test_count_path_reports_sunday_reopen_as_a_second_session(tmp_path) -> None:
    service = AssetBarsService(settings(tmp_path), FakeSynchronizer(session_frame()))

    result = service.get_bars(
        "ICEEUR:BRN1!", "1h", "2026-03-23T00:00:00Z", count=8
    )

    assert result["bars_returned"] == 8
    assert result["sessions_covered"] == 3
    assert result["bars"][-1]["t"] == "2026-03-23T00:00:00Z"


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


def test_finality_requires_successor_without_calendar_evidence() -> None:
    frame = session_frame().iloc[-3:-1]
    duration = pd.Timedelta(hours=1)

    before_delay = finality_flags(
        frame,
        duration,
        pd.Timestamp("2026-03-23T00:24:00Z"),
        pd.Timedelta(minutes=25),
    )
    at_delay = finality_flags(
        frame,
        duration,
        pd.Timestamp("2026-03-23T00:25:00Z"),
        pd.Timedelta(minutes=25),
    )

    assert before_delay.tolist() == [True, False]
    assert at_delay.tolist() == [True, False]


def test_finalization_delay_uses_default_and_exchange_overrides(tmp_path) -> None:
    configured = replace(
        settings(tmp_path),
        finalization_delay_minutes=5,
        finalization_delay_by_exchange={
            "ICEEUR": 25,
            "CME_MINI": 25,
            "COMEX": 25,
        },
    )

    assert finalization_delay(configured, "BITSTAMP:BTCUSD") == pd.Timedelta(minutes=5)
    assert finalization_delay(configured, "ICEEUR:BRN1!") == pd.Timedelta(minutes=25)
    assert finalization_delay(configured, "CME_MINI:NQ1!") == pd.Timedelta(minutes=25)
    assert finalization_delay(configured, "COMEX:GC1!") == pd.Timedelta(minutes=25)


def test_future_request_does_not_include_bar_opening_after_current_time(
    tmp_path, monkeypatch
) -> None:
    index = pd.date_range("2026-07-28T10:00:00Z", periods=3, freq="1h")
    closes = pd.Series([10.0, 11.0, 12.0], index=index)
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": closes + 1,
            "low": closes - 1,
            "close": closes,
            "volume": 1.0,
        },
        index=index,
    )
    monkeypatch.setattr(
        "tv_history.bars.current_utc_time",
        lambda: pd.Timestamp("2026-07-28T11:30:00Z"),
    )
    service = AssetBarsService(settings(tmp_path), FakeSynchronizer(frame))

    result = service.get_bars(
        "BITSTAMP:BTCUSD", "1h", "2026-07-28T13:00:00Z", count=10
    )

    assert [bar["t"] for bar in result["bars"]] == [
        "2026-07-28T10:00:00Z",
        "2026-07-28T11:00:00Z",
    ]
    assert result["bars"][-1]["is_bar_complete"] is False
