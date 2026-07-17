from __future__ import annotations

import pandas as pd

from tv_history.analysis import AssetAnalysisService
from tv_history.config import Settings
from tv_history.storage import CsvStorage


class FakeSynchronizer:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.calls = 0

    def ensure_available(self, asset: str, requested_at: pd.Timestamp):
        self.calls += 1
        return self.frame, {"refreshed": False, "reason": "covered", "bars_requested": 0}


def settings(tmp_path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_root=tmp_path / "data",
        provider_naive_timezone="UTC",
        initial_bars=5000,
        refresh_overlap_bars=10,
        assets=frozenset({"BTCUSD:BITSTAMP"}),
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
            "volume_sma_period": 20,
        },
    )


def hourly_frame() -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=250, freq="1h", tz="UTC", name="timestamp_utc")
    closes = pd.Series([100 + i * 0.1 for i in range(250)], index=index)
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
    service = AssetAnalysisService(configured, sync, CsvStorage(configured))

    result = service.analyze("BTCUSD:BITSTAMP", "1h", "2026-01-11T09:30:00Z")

    assert result["effective_bar_open"] == "2026-01-11T08:00:00+00:00"
    assert result["rsi"]["value"] is not None
    assert result["market_sentiment"]["trend"] == "Bullish"


def test_invalid_timeframe_does_not_synchronize(tmp_path) -> None:
    configured = settings(tmp_path)
    sync = FakeSynchronizer(hourly_frame())
    service = AssetAnalysisService(configured, sync, CsvStorage(configured))

    result = service.analyze("BTCUSD:BITSTAMP", "15m", None)

    assert result["error"] == "invalid_parameter"
    assert sync.calls == 0
