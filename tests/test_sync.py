from __future__ import annotations

import pandas as pd

from tv_history.config import Settings
from tv_history.storage import CsvStorage
from tv_history.sync import HistorySynchronizer


class FakeProvider:
    def __init__(self, frames: list[pd.DataFrame]):
        self.frames = frames
        self.requests: list[tuple[str, int]] = []

    def get_hourly(self, asset: str, n_bars: int) -> pd.DataFrame:
        self.requests.append((asset, n_bars))
        return self.frames.pop(0)


def make_settings(tmp_path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_root=tmp_path / "data",
        provider_naive_timezone="UTC",
        initial_bars=5000,
        refresh_overlap_bars=10,
        assets=frozenset({"BTCUSD:BITSTAMP"}),
        indicators={},
    )


def frame(start: str, closes: list[float]) -> pd.DataFrame:
    index = pd.date_range(start, periods=len(closes), freq="1h", tz="UTC", name="timestamp_utc")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        },
        index=index,
    )


def test_initial_load_requests_5000_bars(tmp_path) -> None:
    settings = make_settings(tmp_path)
    provider = FakeProvider([frame("2026-01-01", [10, 11])])
    sync = HistorySynchronizer(settings, provider, CsvStorage(settings))

    stored, result = sync.ensure_available("BTCUSD:BITSTAMP", pd.Timestamp("2026-01-01T02:00Z"))

    assert provider.requests == [("BTCUSD:BITSTAMP", 5000)]
    assert result["reason"] == "initial_load"
    assert len(stored) == 2


def test_covered_timestamp_does_not_refresh(tmp_path) -> None:
    settings = make_settings(tmp_path)
    provider = FakeProvider([frame("2026-01-01", [10, 11])])
    sync = HistorySynchronizer(settings, provider, CsvStorage(settings))
    sync.ensure_available("BTCUSD:BITSTAMP", pd.Timestamp("2026-01-01T02:00Z"))

    _, result = sync.ensure_available("BTCUSD:BITSTAMP", pd.Timestamp("2026-01-01T00:30Z"))

    assert len(provider.requests) == 1
    assert result["refreshed"] is False


def test_refresh_adds_ten_overlap_and_fresh_rows_replace_by_timestamp(tmp_path) -> None:
    settings = make_settings(tmp_path)
    provider = FakeProvider(
        [
            frame("2026-01-01T00:00Z", [10, 11]),
            frame("2026-01-01T01:00Z", [99, 12, 13]),
        ]
    )
    sync = HistorySynchronizer(settings, provider, CsvStorage(settings))
    sync.ensure_available("BTCUSD:BITSTAMP", pd.Timestamp("2026-01-01T01:00Z"))

    stored, result = sync.ensure_available("BTCUSD:BITSTAMP", pd.Timestamp("2026-01-01T03:00Z"))

    assert provider.requests[-1] == ("BTCUSD:BITSTAMP", 12)
    assert stored.loc[pd.Timestamp("2026-01-01T01:00Z"), "close"] == 99
    assert len(stored) == 4
    assert result["refreshed"] is True
