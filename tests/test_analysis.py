from __future__ import annotations

import pandas as pd

from tv_history.analysis import (
    AssetAnalysisService,
    candle_analysis,
    daily_atr_bands,
    short_term_metrics,
    support_resistance,
)
from tv_history.config import Settings
from tv_history.resample import resample_ohlcv


class FakeSynchronizer:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.calls = 0
        self.requested_timeframes: list[str] = []

    def ensure_available(self, asset: str, timeframe: str, requested_at: pd.Timestamp, **kwargs):
        self.calls += 1
        self.requested_timeframes.append(timeframe)
        direct = self.frame if timeframe == "1h" else resample_ohlcv(self.frame, timeframe)
        return direct, {"refreshed": False, "reason": "covered", "bars_requested": 0}


def settings(tmp_path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_root=tmp_path / "data",
        provider_naive_timezone="UTC",
        initial_bars=5000,
        refresh_overlap_bars=10,
        indicators={
            "sma": [20, 50, 200],
            "ema": [9, 20, 50, 200],
            "rsi_period": 14,
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
            "bollinger_period": 20,
            "bollinger_stddev": 2.0,
            "atr_period": 14,
            "atr_daily_band_coefficient": 0.4,
            "adx_period": 14,
            "volume_sma_period": 20,
        },
    )


def hourly_frame() -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=1000, freq="1h", tz="UTC", name="timestamp_utc")
    closes = pd.Series([100 + i * 0.1 for i in range(1000)], index=index)
    return pd.DataFrame(
        {
            "open": closes - 0.05,
            "high": closes + 0.2,
            "low": closes - 0.2,
            "close": closes,
            "volume": 1000.0,
        },
        index=index,
    )


def test_asset_analysis_returns_last_completed_bar(tmp_path) -> None:
    configured = settings(tmp_path)
    sync = FakeSynchronizer(hourly_frame())
    service = AssetAnalysisService(configured, sync)

    result = service.analyze("BTCUSD:BITSTAMP", "1h", "2026-02-10T09:30:00Z")

    for removed in ("effective_bar_open", "source", "refresh", "storage", "market_sentiment", "macd"):
        assert removed not in result
    assert result["effective_bar_close"] == "2026-02-10T09:00:00+00:00"
    assert result["bar_open"] == "2026-02-10T08:00:00Z"
    assert result["bar_close"] == "2026-02-10T09:00:00Z"
    assert result["is_bar_complete"] is True
    assert "macd_12_26" not in result
    assert "trend_state" in result
    assert result["volatility"]["atr_14"] is not None
    assert result["volume_analysis"]["current"] == result["price_data"]["volume"]
    assert result["levels"]


def test_live_analysis_returns_latest_received_bar_as_incomplete_price_data(
    tmp_path, monkeypatch
) -> None:
    configured = settings(tmp_path)
    frame = hourly_frame()
    now = pd.Timestamp("2026-02-10T09:10:00Z")
    monkeypatch.setattr("tv_history.analysis.current_utc_time", lambda: now)
    monkeypatch.setattr("tv_history.analysis.parse_timestamp", lambda _value: now)
    service = AssetAnalysisService(configured, FakeSynchronizer(frame))

    result = service.analyze("BTCUSD:BITSTAMP", "1h", None)

    provisional = frame.loc[pd.Timestamp("2026-02-10T09:00:00Z")]
    finalized = frame.loc[pd.Timestamp("2026-02-10T08:00:00Z")]
    assert result["bar_open"] == "2026-02-10T09:00:00Z"
    assert result["bar_close"] == "2026-02-10T10:00:00Z"
    assert result["is_bar_complete"] is False
    assert result["price_data"]["close"] == round(float(provisional["close"]), 2)
    assert result["volume_analysis"]["current"] == round(float(finalized["volume"]), 2)


def test_live_analysis_keeps_latest_bar_incomplete_after_exchange_delay(
    tmp_path, monkeypatch
) -> None:
    configured = settings(tmp_path)
    frame = hourly_frame().loc[:"2026-02-10T09:00:00Z"]
    now = pd.Timestamp("2026-02-10T10:05:00Z")
    monkeypatch.setattr("tv_history.analysis.current_utc_time", lambda: now)
    monkeypatch.setattr("tv_history.analysis.parse_timestamp", lambda _value: now)

    result = AssetAnalysisService(configured, FakeSynchronizer(frame)).analyze(
        "BTCUSD:BITSTAMP", "1h", None
    )

    assert result["bar_open"] == "2026-02-10T09:00:00Z"
    assert result["is_bar_complete"] is False
    # Elapsed time alone cannot confirm an ordinary latest bar.
    assert result["volume_analysis"]["current"] == 1000.0


def test_invalid_timeframe_does_not_synchronize(tmp_path) -> None:
    configured = settings(tmp_path)
    sync = FakeSynchronizer(hourly_frame())
    service = AssetAnalysisService(configured, sync)

    result = service.analyze("BTCUSD:BITSTAMP", "15m", None)

    assert result["error"]["code"] == "UNSUPPORTED_TIMEFRAME"
    assert sync.calls == 0


def test_dynamic_asset_is_not_restricted_by_configuration(tmp_path) -> None:
    configured = settings(tmp_path)
    sync = FakeSynchronizer(hourly_frame())
    service = AssetAnalysisService(configured, sync)

    result = service.analyze("ETHUSD:BITSTAMP", "1h", "2026-02-10T09:30:00Z")

    assert result["asset"] == "ETHUSD:BITSTAMP"
    assert sync.calls == 2
    assert sync.requested_timeframes == ["1h", "1D"]


def test_tradingview_exchange_symbol_format_is_accepted(tmp_path) -> None:
    configured = settings(tmp_path)
    sync = FakeSynchronizer(hourly_frame())
    service = AssetAnalysisService(configured, sync)

    result = service.analyze("RUS:MX1!", "1h", "2026-02-10T09:30:00Z")

    assert result["asset"] == "RUS:MX1!"
    assert sync.calls == 2


def test_unsafe_asset_is_rejected_before_synchronization(tmp_path) -> None:
    configured = settings(tmp_path)
    sync = FakeSynchronizer(hourly_frame())
    service = AssetAnalysisService(configured, sync)

    result = service.analyze("BTCUSD:..\\escape", "1h", None)

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert sync.calls == 0


def test_weekly_support_resistance_uses_classic_formulas() -> None:
    index = pd.date_range("2026-01-05", periods=8 * 24, freq="1h", tz="UTC", name="timestamp_utc")
    frame = pd.DataFrame(
        {"open": 100.0, "high": 110.0, "low": 90.0, "close": 100.0, "volume": 1.0},
        index=index,
    )

    result = support_resistance(
        frame, "1h", pd.Timestamp("2026-01-13T00:00:00Z"), current_close=105.0
    )

    assert result["pivot"] == 100.0
    assert result["resistance_1"] == 110.0
    assert result["resistance_2"] == 120.0
    assert result["resistance_3"] == 140.0
    assert result["support_1"] == 90.0
    assert result["support_2"] == 80.0
    assert result["support_3"] == 60.0
    assert result["nearest_resistance"] == 110.0
    assert result["nearest_support"] == 90.0


def test_daily_support_resistance_uses_previous_completed_month() -> None:
    index = pd.date_range("2026-01-01", "2026-02-02", freq="1h", tz="UTC", name="timestamp_utc")
    frame = pd.DataFrame(
        {"open": 100.0, "high": 110.0, "low": 90.0, "close": 100.0, "volume": 1.0},
        index=index,
    )

    result = support_resistance(
        frame, "1D", pd.Timestamp("2026-02-02T00:00:00Z"), current_close=105.0
    )

    assert result["pivot"] == 100.0
    assert result["resistance_1"] == 110.0
    assert result["support_1"] == 90.0


def test_daily_atr_bands_use_configured_coefficient(tmp_path) -> None:
    configured = settings(tmp_path)
    daily = resample_ohlcv(hourly_frame(), "1D")

    result = daily_atr_bands(
        daily, pd.Timestamp("2026-02-10T09:30:00Z"), configured.indicators
    )

    assert result["value"] is not None
    midpoint = (result["atr_upper"] + result["atr_lower"]) / 2
    half_width = (result["atr_upper"] - result["atr_lower"]) / 2
    assert abs(half_width - 0.4 * result["value"]) <= 0.02
    assert midpoint > 0


def test_small_candle_is_neutral_by_timeframe() -> None:
    row = pd.Series({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.4})
    assert candle_analysis(row, "1h")["type"] == "Neutral"

    row["close"] = 100.8
    assert candle_analysis(row, "4h")["type"] == "Neutral"

    row["close"] = 101.5
    assert candle_analysis(row, "1D")["type"] == "Neutral"

    row["high"] = 103.0
    row["close"] = 102.1
    assert candle_analysis(row, "1D")["type"] == "Bullish"


def test_short_term_efficiency_is_null_for_flat_path() -> None:
    index = pd.date_range("2026-01-01", periods=20, freq="1h", tz="UTC")
    frame = pd.DataFrame(
        {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 1.0},
        index=index,
    )

    result = short_term_metrics(frame, 1, row_atr=2.0)

    assert result["efficiency_ratio"] is None


def test_analysis_error_shape_is_identical_and_dst_error_is_not_retryable(tmp_path) -> None:
    class AmbiguousSynchronizer:
        def ensure_available(self, asset: str, timeframe: str, requested_at: pd.Timestamp):
            raise ValueError("2013-10-27 03:00:00 is an ambiguous time and cannot be inferred")

    service = AssetAnalysisService(settings(tmp_path), AmbiguousSynchronizer())

    default = service.analyze("BITSTAMP:BTCUSD", "4h", "2026-01-12T04:00:00Z")
    explicit = service.analyze(
        "BITSTAMP:BTCUSD", "4h", "2026-01-12T04:00:00Z", "execution"
    )

    assert default == explicit
    assert default["error"]["code"] == "DATA_PROVIDER_ERROR"
    assert default["error"]["retryable"] is False
