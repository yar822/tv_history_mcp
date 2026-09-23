from __future__ import annotations

import json

import pandas as pd

from tv_history.analysis import AssetAnalysisService
from tv_history.execution import (
    classify_trend_state,
    data_quality,
    last_structure_event,
    market_structure,
    session_context,
    week_to_date,
    weekly_vwap,
)

from test_analysis import FakeSynchronizer, hourly_frame, settings


def test_execution_response_uses_completed_4h_bars_without_future_leakage(tmp_path) -> None:
    configured = settings(tmp_path)
    original = hourly_frame()
    with_changed_future = original.copy()
    future_rows = with_changed_future.index >= pd.Timestamp("2026-02-10T09:00:00Z")
    with_changed_future.loc[future_rows, ["open", "high", "low", "close"]] += 10000
    requested_at = "2026-02-10T09:30:00Z"

    baseline = AssetAnalysisService(configured, FakeSynchronizer(original)).analyze(
        "BTCUSD:BITSTAMP", "4h", requested_at, "execution"
    )
    changed = AssetAnalysisService(configured, FakeSynchronizer(with_changed_future)).analyze(
        "BTCUSD:BITSTAMP", "4h", requested_at, "execution"
    )

    assert baseline == changed
    assert baseline["effective_bar_close"] == "2026-02-10T08:00:00+00:00"
    assert pd.Timestamp(baseline["effective_bar_close"]) <= pd.Timestamp(requested_at)
    assert baseline["bar_open"] == "2026-02-10T04:00:00Z"
    assert baseline["bar_close"] == "2026-02-10T08:00:00Z"
    assert baseline["is_bar_complete"] is True
    assert baseline["volatility"]["atr_14"] is not None
    assert baseline["volatility"]["daily_atr_14"] is not None
    assert baseline["volatility"]["atr_14"] != baseline["volatility"]["daily_atr_14"]


def test_live_execution_exposes_provisional_price_without_using_it_in_metrics(
    tmp_path, monkeypatch
) -> None:
    configured = settings(tmp_path)
    original = hourly_frame()
    changed = original.copy()
    now = pd.Timestamp("2026-02-10T09:10:00Z")
    provisional = changed.index == pd.Timestamp("2026-02-10T09:00:00Z")
    changed.loc[provisional, ["open", "high", "low", "close", "volume"]] *= 10
    monkeypatch.setattr("tv_history.analysis.current_utc_time", lambda: now)
    monkeypatch.setattr("tv_history.analysis.parse_timestamp", lambda _value: now)

    baseline = AssetAnalysisService(configured, FakeSynchronizer(original)).analyze(
        "BTCUSD:BITSTAMP", "1h", None, "execution"
    )
    modified = AssetAnalysisService(configured, FakeSynchronizer(changed)).analyze(
        "BTCUSD:BITSTAMP", "1h", None, "execution"
    )

    assert baseline["bar_open"] == "2026-02-10T09:00:00Z"
    assert baseline["is_bar_complete"] is False
    assert baseline["price_data"] != modified["price_data"]
    assert baseline["previous_bar"]["close"] == round(
        float(original.loc[pd.Timestamp("2026-02-10T08:00:00Z"), "close"]), 2
    )
    for field in (
        "volume_analysis",
        "candle",
        "previous_bar",
        "volatility",
        "recent_path",
        "structure",
        "trend_state",
        "levels",
        "bar_signal",
        "level_interaction",
        "week_to_date",
        "weekly_vwap",
        "data_quality",
    ):
        assert baseline[field] == modified[field]


def test_execution_response_is_default_and_raw_indicators_are_opt_in(tmp_path) -> None:
    configured = settings(tmp_path)
    frame = hourly_frame()
    service = AssetAnalysisService(configured, FakeSynchronizer(frame))

    default = service.analyze("BTCUSD:BITSTAMP", "1h", "2026-02-10T09:30:00Z")
    execution = service.analyze(
        "BTCUSD:BITSTAMP", "1h", "2026-02-10T09:30:00Z", "execution"
    )
    with_indicators = service.analyze(
        "BTCUSD:BITSTAMP", "1h", "2026-02-10T09:30:00Z", "execution", True
    )

    assert default == execution
    for hidden in ("rsi", "macd_12_26", "sma", "ema", "bollinger_bands", "adx", "momentum_change"):
        assert hidden not in default
        assert hidden in with_indicators
    assert "signals" not in with_indicators["ema"]
    assert set(with_indicators["ema"]) == {"ema9", "ema20", "ema50"}
    assert set(with_indicators["rsi"]) == {"value", "previous"}
    assert "position" not in with_indicators["bollinger_bands"]
    assert "signal" not in execution["volume_analysis"]
    assert "type" not in execution["candle"]
    assert "support_resistance" not in execution
    assert len(json.dumps(default)) < len(json.dumps(with_indicators))


def test_analysis_rejects_removed_legacy_response(tmp_path) -> None:
    result = AssetAnalysisService(
        settings(tmp_path), FakeSynchronizer(hourly_frame())
    ).analyze("BTCUSD:BITSTAMP", "1h", "2026-02-10T09:30:00Z", "legacy")

    assert result["error"]["code"] == "INVALID_PARAMETER"


def test_execution_returns_structured_error_when_history_is_insufficient(tmp_path) -> None:
    configured = settings(tmp_path)
    short = hourly_frame().iloc[:5]
    result = AssetAnalysisService(configured, FakeSynchronizer(short)).analyze(
        "BTCUSD:BITSTAMP", "1h", "2026-01-01T05:00:00Z", "execution"
    )

    assert result["error"]["code"] == "INSUFFICIENT_DATA"
    assert result["error"]["retryable"] is False
    assert result["error"]["bars_available"] == 4


def test_week_to_date_resets_monday_and_excludes_later_observations() -> None:
    index = pd.to_datetime(
        [
            "2026-07-19T23:00:00Z",
            "2026-07-20T00:00:00Z",
            "2026-07-20T01:00:00Z",
            "2026-07-20T02:00:00Z",
        ]
    )
    frame = pd.DataFrame(
        {
            "open": [90.0, 100.0, 101.0, 500.0],
            "high": [91.0, 102.0, 103.0, 501.0],
            "low": [89.0, 99.0, 100.0, 499.0],
            "close": [90.0, 101.0, 102.0, 500.0],
            "volume": [10.0, 10.0, 10.0, 10.0],
        },
        index=index,
    )
    requested_at = pd.Timestamp("2026-07-20T02:00:00Z")
    completed = frame.loc[(frame.index + pd.Timedelta(hours=1)) <= requested_at]

    result = week_to_date(completed, requested_at, "1h")

    assert result["source_timeframe"] == "1h"
    assert result["week_open"] == 100.0
    assert result["highest_close"] == 102.0
    assert result["lowest_close"] == 101.0


def test_failed_downside_breakout_is_detected_as_reclaim() -> None:
    index = pd.date_range("2026-07-20", periods=7, freq="1h", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": [10.0, 9.0, 8.0, 9.0, 10.0, 7.0, 8.0],
            "high": [10.5, 9.5, 8.5, 9.5, 10.5, 7.2, 9.5],
            "low": [9.5, 8.5, 7.0, 8.5, 9.5, 6.0, 8.0],
            "close": [10.0, 9.0, 8.0, 9.0, 10.0, 6.5, 9.0],
            "volume": [100.0] * 7,
        },
        index=index,
    )

    result = market_structure(frame, atr=1.0)

    assert result["active_breakout"] is None
    assert result["last_structure_event"]["type"] == "failed_breakout_below"
    assert result["last_structure_event"]["level"] == 7.0
    assert result["last_structure_event"]["bars_ago"] == 0


def test_data_quality_detects_gaps_and_unreliable_volume() -> None:
    index = pd.to_datetime(["2026-07-20T00:00:00Z", "2026-07-20T02:00:00Z"])
    frame = pd.DataFrame(
        {
            "open": [1.0, 1.0],
            "high": [2.0, 2.0],
            "low": [0.0, 0.0],
            "close": [1.0, 1.0],
            "volume": [0.0, 0.0],
        },
        index=index,
    )

    quality = data_quality(frame, "1h", "BTCUSD:BITSTAMP")
    vwap = weekly_vwap(frame, pd.Timestamp("2026-07-20T03:00:00Z"), 1.0, 1.0)

    assert quality == {
        "bars_available": 2,
        "unexpected_missing_bars": True,
        "scheduled_session_gaps": 0,
        "volume_reliable": False,
    }
    assert vwap == {"value": None, "distance_pct": None, "distance_atr": None}


def test_gap_and_close_to_close_returns_are_separate(tmp_path) -> None:
    configured = settings(tmp_path)
    frame = hourly_frame().iloc[:60].copy()
    frame.loc[frame.index[-2], "close"] = 100.0
    frame.loc[frame.index[-1], ["open", "high", "low", "close"]] = [90.0, 92.0, 89.0, 91.0]
    frame = pd.concat([frame, hourly_frame().iloc[60:61]])  # observed successor
    result = AssetAnalysisService(configured, FakeSynchronizer(frame)).analyze(
        "BRN1!:ICEEUR", "1h", "2026-01-03T12:00:00Z", "execution"
    )

    assert result["price_data"]["change_open_to_close_pct"] == 1.11
    assert result["price_data"]["change_from_previous_close_pct"] == -9.0
    assert result["price_data"]["opening_gap_pct"] == -10.0
    assert "change_percent" not in result["price_data"]


def test_recent_failure_is_preserved_when_new_breakout_occurs() -> None:
    events = [
        {
            "type": "failed_breakout_above",
            "level": 100.0,
            "position": 8,
            "time": pd.Timestamp("2026-07-20T08:00:00Z"),
        },
        {
            "type": "breakout_below",
            "level": 95.0,
            "position": 10,
            "time": pd.Timestamp("2026-07-20T10:00:00Z"),
        },
    ]

    result = last_structure_event(events, frame_length=11)

    assert result["type"] == "failed_breakout_above"
    assert result["bars_ago"] == 2


def test_futures_weekend_gap_is_not_reported_as_unexpected() -> None:
    index = pd.to_datetime(["2026-07-17T20:00:00Z", "2026-07-20T00:00:00Z"])
    frame = pd.DataFrame(
        {
            "open": [1.0, 1.0],
            "high": [2.0, 2.0],
            "low": [0.0, 0.0],
            "close": [1.0, 1.0],
            "volume": [1.0, 1.0],
        },
        index=index,
    )

    result = data_quality(frame, "4h", "BRN1!:ICEEUR")

    assert result["unexpected_missing_bars"] is None
    assert result["scheduled_session_gaps"] == 0  # A weekend alone is not verified evidence.

    crypto = data_quality(frame, "4h", "BTCUSD:BITSTAMP")
    assert crypto["unexpected_missing_bars"] is True
    assert crypto["scheduled_session_gaps"] == 0


def test_daily_execution_has_eighty_bar_warmup(tmp_path) -> None:
    configured = settings(tmp_path)
    index = pd.date_range("2025-09-01", periods=120 * 24, freq="1h", tz="UTC")
    closes = pd.Series([100 + index_value * 0.01 for index_value in range(len(index))], index=index)
    frame = pd.DataFrame(
        {
            "open": closes - 0.02,
            "high": closes + 0.05,
            "low": closes - 0.05,
            "close": closes,
            "volume": 100.0,
        },
        index=index,
    )
    result = AssetAnalysisService(configured, FakeSynchronizer(frame)).analyze(
        "BTCUSD:BITSTAMP", "1D", "2025-12-20T20:00:00Z", "execution", True
    )

    assert result["data_quality"]["bars_available"] >= 80
    assert result["macd_12_26"]["macd_line"] is not None
    assert result["macd_12_26"]["signal_line"] is not None
    assert result["ema"]["ema50"] is not None
    assert result["adx"]["value"] is not None
    assert result["momentum_change"]["previous_macd_histogram"] is not None


def test_execution_unsupported_timeframe_is_structured_error(tmp_path) -> None:
    result = AssetAnalysisService(settings(tmp_path), FakeSynchronizer(hourly_frame())).analyze(
        "BTCUSD:BITSTAMP", "15m", None, "execution"
    )

    assert result["error"]["code"] == "UNSUPPORTED_TIMEFRAME"
    assert result["error"]["retryable"] is False


def test_execution_asset_not_found_is_structured_error(tmp_path) -> None:
    class MissingAssetSynchronizer:
        def ensure_available(
            self, asset: str, timeframe: str, requested_at: pd.Timestamp, **kwargs
        ):
            raise RuntimeError(f"tvDatafeed returned no data for {asset}")

    result = AssetAnalysisService(settings(tmp_path), MissingAssetSynchronizer()).analyze(
        "BRN1:ICEEUR", "4h", "2026-04-24T04:00:00Z", "execution"
    )

    assert result["error"] == {
        "code": "ASSET_NOT_FOUND",
        "message": "tvDatafeed returned no data for BRN1:ICEEUR",
        "retryable": False,
    }


def test_direct_daily_source_excludes_rows_after_request(tmp_path) -> None:
    configured = settings(tmp_path)
    hourly = hourly_frame()
    daily_index = pd.date_range("2025-09-01", periods=150, freq="1D", tz="UTC")
    closes = pd.Series([80_000 + value * 10 for value in range(150)], index=daily_index)
    daily = pd.DataFrame(
        {
            "open": closes - 5,
            "high": closes + 20,
            "low": closes - 20,
            "close": closes,
            "volume": 1000.0,
        },
        index=daily_index,
    )
    changed = daily.copy()
    requested_at = pd.Timestamp("2026-01-14T20:00:00Z")
    changed.loc[changed.index >= requested_at, ["open", "high", "low", "close"]] += 1_000_000

    class DirectDailySynchronizer:
        def __init__(self, direct_daily: pd.DataFrame):
            self.direct_daily = direct_daily

        def ensure_available(
            self, asset: str, timeframe: str, requested: pd.Timestamp, **kwargs
        ):
            assert timeframe == "1D"
            return self.direct_daily, {"refreshed": False}

    baseline = AssetAnalysisService(configured, DirectDailySynchronizer(daily)).analyze(
        "BTCUSD:BITSTAMP", "1D", requested_at.isoformat(), "execution"
    )
    future_changed = AssetAnalysisService(configured, DirectDailySynchronizer(changed)).analyze(
        "BTCUSD:BITSTAMP", "1D", requested_at.isoformat(), "execution"
    )

    assert baseline == future_changed
    assert pd.Timestamp(baseline["effective_bar_close"]) <= requested_at


def test_execution_levels_signal_and_interaction_are_compact_and_structured(tmp_path) -> None:
    result = AssetAnalysisService(settings(tmp_path), FakeSynchronizer(hourly_frame())).analyze(
        "BTCUSD:BITSTAMP", "4h", "2026-02-10T09:30:00Z", "execution"
    )

    assert len(result["levels"]["supports"]) <= 2
    assert len(result["levels"]["resistances"]) <= 2
    for level in result["levels"]["supports"] + result["levels"]["resistances"]:
        assert set(level) == {"price", "distance_atr", "source", "touches"}
        assert level["source"] in {"swing", "weekly", "breakout", "pivot"}
    assert set(result["bar_signal"]) == {
        "type", "direction", "level", "confirmation_count", "confidence"
    }
    assert result["bar_signal"]["type"] in {
        "bullish_rejection", "bearish_rejection", "bullish_reversal",
        "bearish_reversal", "breakout", "failed_breakout", "reclaim", "none",
    }
    assert result["bar_signal"]["direction"] in {"up", "down", "neutral"}
    assert set(result["level_interaction"]) == {
        "level", "event", "distance_atr", "consecutive_closes_beyond"
    }
    assert result["level_interaction"]["event"] in {
        "approach", "touch", "close_beyond", "reclaim", "reject", None
    }


def test_futures_session_uses_prior_completed_bar_cadence() -> None:
    closes = pd.to_datetime(
        [
            "2026-07-10T20:00:00Z",
            "2026-07-13T00:00:00Z",
            "2026-07-13T04:00:00Z",
            "2026-07-17T20:00:00Z",
        ]
    )
    index = closes - pd.Timedelta(hours=4)
    frame = pd.DataFrame(
        {
            "open": [1.0] * 4,
            "high": [2.0] * 4,
            "low": [0.0] * 4,
            "close": [1.0] * 4,
            "volume": [1.0] * 4,
        },
        index=index,
    )

    result = session_context(frame, "4h", "BRN1!:ICEEUR")

    assert result == {
        "next_expected_bar_close": "2026-07-20T00:00:00Z",
        "gap_expected": True,
    }
    assert session_context(frame, "4h", "BTCUSD:BITSTAMP") is None


def trend_frame(closes: list[float] | None = None) -> pd.DataFrame:
    values = closes or [112.0] * 20
    index = pd.date_range("2026-07-20", periods=len(values), freq="1h", tz="UTC")
    return pd.DataFrame({"close": values}, index=index)


def trend_swings(highs: list[float], lows: list[float]) -> list[tuple]:
    high_positions = [2, 6, 10, 14][:len(highs)]
    low_positions = [4, 8, 12, 16][:len(lows)]
    index = trend_frame().index
    swings = [
        ("high", position, price, index[position])
        for position, price in zip(high_positions, highs)
    ] + [
        ("low", position, price, index[position])
        for position, price in zip(low_positions, lows)
    ]
    return sorted(swings, key=lambda swing: swing[1])


def test_trend_state_classifies_rising_and_falling_confirmed_swings() -> None:
    up = classify_trend_state(
        trend_frame(), trend_swings([100.0, 102.0, 104.0], [90.0, 92.0, 94.0]), 1.0, {}
    )
    down = classify_trend_state(
        trend_frame(), trend_swings([104.0, 102.0, 100.0], [94.0, 92.0, 90.0]), 1.0, {}
    )

    assert up == {
        "direction": "UP",
        "confidence": 0.75,
        "higher_highs": True,
        "higher_lows": True,
        "trend_change_confirmed": False,
    }
    assert down == {
        "direction": "DOWN",
        "confidence": 0.75,
        "higher_highs": False,
        "higher_lows": False,
        "trend_change_confirmed": False,
    }


def test_trend_state_returns_side_for_mixed_overlap_and_noise() -> None:
    mixed = classify_trend_state(
        trend_frame(), trend_swings([100.0, 102.0], [92.0, 90.0]), 1.0, {}
    )
    overlapping = classify_trend_state(
        trend_frame(), trend_swings([100.0, 100.2], [90.0, 90.2]), 1.0, {}
    )
    noise = classify_trend_state(
        trend_frame(), trend_swings([100.0, 100.05], [90.0, 90.05]), 1.0, {}
    )

    assert mixed["direction"] == "SIDE"
    assert overlapping["direction"] == "SIDE"
    assert noise == {
        "direction": "SIDE",
        "confidence": 0.55,
        "higher_highs": False,
        "higher_lows": False,
        "trend_change_confirmed": False,
    }


def test_trend_state_missing_structure_returns_required_default() -> None:
    result = classify_trend_state(
        trend_frame(), trend_swings([100.0], [90.0]), 1.0, {}
    )

    assert result == {
        "direction": "SIDE",
        "confidence": 0.5,
        "higher_highs": False,
        "higher_lows": False,
        "trend_change_confirmed": False,
    }


def test_trend_change_requires_protected_swing_close_confirmation() -> None:
    swings = trend_swings(
        [100.0, 110.0, 120.0, 115.0],
        [90.0, 100.0, 110.0, 105.0],
    )
    marginal_closes = [112.0] * 20
    marginal_closes[17] = 109.5
    marginal = classify_trend_state(trend_frame(marginal_closes), swings, 1.0, {})

    confirmed_closes = marginal_closes.copy()
    confirmed_closes[18] = 109.4
    confirmed = classify_trend_state(trend_frame(confirmed_closes), swings, 1.0, {})

    deep_close = [112.0] * 20
    deep_close[17] = 108.9
    one_atr = classify_trend_state(trend_frame(deep_close), swings, 1.0, {})

    assert marginal["direction"] == "DOWN"
    assert marginal["trend_change_confirmed"] is False
    assert confirmed["direction"] == "DOWN"
    assert confirmed["trend_change_confirmed"] is True
    assert one_atr["trend_change_confirmed"] is True


def test_trend_confidence_requires_agreeing_structure_and_drops_for_opposition() -> None:
    swings = trend_swings([100.0, 102.0, 104.0], [90.0, 92.0, 94.0])
    agreeing = classify_trend_state(
        trend_frame(),
        swings,
        1.0,
        {"active_breakout": {"direction": "above"}, "last_structure_event": None},
    )
    opposing = classify_trend_state(
        trend_frame(),
        swings,
        1.0,
        {"active_breakout": {"direction": "below"}, "last_structure_event": None},
    )

    assert agreeing["confidence"] == 0.85
    assert opposing["confidence"] == 0.6


def test_trend_state_is_present_in_default_execution_response(tmp_path) -> None:
    service = AssetAnalysisService(settings(tmp_path), FakeSynchronizer(hourly_frame()))
    default = service.analyze("BTCUSD:BITSTAMP", "4h", "2026-02-10T09:30:00Z")
    execution = service.analyze(
        "BTCUSD:BITSTAMP", "4h", "2026-02-10T09:30:00Z", "execution"
    )

    assert default == execution
    assert set(execution["trend_state"]) == {
        "direction", "confidence", "higher_highs", "higher_lows",
        "trend_change_confirmed",
    }
    assert execution["price_data"]["close"] == 196.7
    assert execution["effective_bar_close"] == "2026-02-10T08:00:00+00:00"


def test_weekly_execution_uses_direct_weekly_and_daily_sources(tmp_path) -> None:
    daily_index = pd.date_range("2024-12-01", periods=420, freq="1D", tz="UTC")
    closes = pd.Series([50_000 + value * 5 for value in range(420)], index=daily_index)
    daily = pd.DataFrame(
        {
            "open": closes - 2,
            "high": closes + 10,
            "low": closes - 10,
            "close": closes,
            "volume": 1000.0,
        },
        index=daily_index,
    )

    weekly = daily.resample("W-MON", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )

    class DirectWeeklySynchronizer:
        def ensure_available(
            self, asset: str, timeframe: str, requested: pd.Timestamp, **kwargs
        ):
            return (weekly if timeframe == "1W" else daily), {"refreshed": False}

    result = AssetAnalysisService(
        settings(tmp_path), DirectWeeklySynchronizer()
    ).analyze("BTCUSD:BITSTAMP", "1W", "2026-01-14T20:00:00Z", "execution")

    assert "error" not in result
    assert result["data_quality"]["bars_available"] >= 50
    assert set(result["trend_state"]) == {
        "direction", "confidence", "higher_highs", "higher_lows",
        "trend_change_confirmed",
    }
