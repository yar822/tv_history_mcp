from __future__ import annotations

import pandas as pd
import pytest

from tv_history.config import Settings
from tv_history.provider import TvDatafeedProvider, normalize_asset, split_asset
from tv_history.storage import CsvStorage
from tv_history.sync import HistorySynchronizer


class FakeProvider:
    def __init__(self, frames: list[pd.DataFrame]):
        self.frames = frames
        self.requests: list[tuple[str, str, int]] = []

    def get_history(self, asset: str, timeframe: str, n_bars: int) -> pd.DataFrame:
        self.requests.append((asset, timeframe, n_bars))
        return self.frames.pop(0)


def make_settings(tmp_path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_root=tmp_path / "data",
        provider_naive_timezone="UTC",
        initial_bars=5000,
        refresh_overlap_bars=10,
        indicators={},
    )


def frame(start: str, closes: list[float], freq: str = "1h") -> pd.DataFrame:
    index = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC", name="timestamp_utc")
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

    stored, result = sync.ensure_available(
        "BTCUSD:BITSTAMP", "4h", pd.Timestamp("2026-01-01T08:00Z")
    )

    assert provider.requests == [("BTCUSD:BITSTAMP", "4h", 5000)]
    assert result["reason"] == "initial_load"
    assert len(stored) == 2
    control = pd.read_csv(settings.data_root / "download_control.csv")
    assert control.loc[0, "asset"] == "BTCUSD:BITSTAMP"
    assert control.loc[0, "timeframe"] == "4h"
    assert control.loc[0, "requested_at"] == "2026-01-01T08:00:00+00:00"
    assert control.loc[0, "bars_requested"] == 5000
    assert control.loc[0, "status"] == "success"
    assert pd.notna(control.loc[0, "input_timestamp"])


def test_covered_timestamp_does_not_refresh(tmp_path) -> None:
    settings = make_settings(tmp_path)
    provider = FakeProvider([frame("2026-01-01", [10, 11])])
    sync = HistorySynchronizer(settings, provider, CsvStorage(settings))
    sync.ensure_available("BTCUSD:BITSTAMP", "1h", pd.Timestamp("2026-01-01T02:00Z"))

    _, result = sync.ensure_available(
        "BTCUSD:BITSTAMP", "1h", pd.Timestamp("2026-01-01T00:30Z")
    )

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
    sync.ensure_available("BTCUSD:BITSTAMP", "1h", pd.Timestamp("2026-01-01T01:00Z"))

    stored, result = sync.ensure_available(
        "BTCUSD:BITSTAMP", "1h", pd.Timestamp("2026-01-01T03:00Z")
    )

    assert provider.requests[-1] == ("BTCUSD:BITSTAMP", "1h", 12)
    assert stored.loc[pd.Timestamp("2026-01-01T01:00Z"), "close"] == 99
    assert len(stored) == 4
    assert result["refreshed"] is True


def test_failed_download_is_recorded(tmp_path) -> None:
    class FailingProvider:
        def get_history(self, asset: str, timeframe: str, n_bars: int) -> pd.DataFrame:
            raise RuntimeError("provider unavailable")

    settings = make_settings(tmp_path)
    sync = HistorySynchronizer(settings, FailingProvider(), CsvStorage(settings))

    with pytest.raises(RuntimeError, match="provider unavailable"):
        sync.ensure_available(
            "ETHUSD:BITSTAMP", "1W", pd.Timestamp("2026-01-01T02:00Z")
        )

    control = pd.read_csv(settings.data_root / "download_control.csv")
    assert control.loc[0, "asset"] == "ETHUSD:BITSTAMP"
    assert control.loc[0, "timeframe"] == "1W"
    assert control.loc[0, "bars_requested"] == 5000
    assert control.loc[0, "status"] == "failure"


def test_storage_path_cannot_escape_data_root(tmp_path) -> None:
    settings = make_settings(tmp_path)
    storage = CsvStorage(settings)

    with pytest.raises(ValueError, match="path separators"):
        storage.path_for("BTCUSD:..\\escape")


def test_asset_formats_resolve_to_symbol_and_exchange(tmp_path) -> None:
    assert split_asset("RUS:MX1!") == ("MX1!", "RUS")
    assert split_asset("BTCUSD:BITSTAMP") == ("BTCUSD", "BITSTAMP")
    assert normalize_asset("rus:mx1!") == "RUS:MX1!"

    path = CsvStorage(make_settings(tmp_path)).path_for("RUS:MX1!", "4h")
    assert path.parent.parent.name == "RUS"
    assert path.name == "4h.csv"


def test_provider_sends_exchange_symbol_input_in_tvdatafeed_argument_order(tmp_path) -> None:
    class FakeClient:
        def __init__(self):
            self.arguments = None

        def get_hist(self, **kwargs):
            self.arguments = kwargs
            result = frame("2026-01-01", [10.0])
            result.index = result.index.tz_localize(None)
            return result

    provider = TvDatafeedProvider(make_settings(tmp_path))
    provider._client = FakeClient()

    provider.get_history("RUS:MX1!", "4h", 25)

    assert provider._client.arguments["symbol"] == "MX1!"
    assert provider._client.arguments["exchange"] == "RUS"
    assert provider._client.arguments["n_bars"] == 25


def test_each_timeframe_is_downloaded_cached_and_stored_separately(tmp_path) -> None:
    settings = make_settings(tmp_path)
    four_hour = frame("2026-01-01", [100, 101], "4h")
    weekly = frame("2025-12-22", [90, 100], "7D")
    provider = FakeProvider([four_hour, weekly])
    storage = CsvStorage(settings)
    sync = HistorySynchronizer(settings, provider, storage)

    first, _ = sync.ensure_available(
        "BTCUSD:BITSTAMP", "4h", pd.Timestamp("2026-01-01T08:00Z")
    )
    second, _ = sync.ensure_available(
        "BTCUSD:BITSTAMP", "1W", pd.Timestamp("2026-01-05T00:00Z")
    )

    assert len(first) == 2
    assert len(second) == 2
    assert storage.path_for("BTCUSD:BITSTAMP", "4h").exists()
    assert storage.path_for("BTCUSD:BITSTAMP", "1W").exists()
    assert not storage.path_for("BTCUSD:BITSTAMP", "1h").exists()


def test_download_control_migrates_old_schema(tmp_path) -> None:
    settings = make_settings(tmp_path)
    settings.data_root.mkdir(parents=True)
    control_path = settings.data_root / "download_control.csv"
    control_path.write_text(
        "asset,requested_at,bars_requested,status,input_timestamp\n"
        "BTCUSD:BITSTAMP,2026-01-01T00:00:00+00:00,10,success,2026-01-01T00:01:00+00:00\n",
        encoding="utf-8",
    )
    provider = FakeProvider([frame("2026-01-01", [10])])
    sync = HistorySynchronizer(settings, provider, CsvStorage(settings))

    sync.ensure_available("ETHUSD:BITSTAMP", "1D", pd.Timestamp("2026-01-02T00:00Z"))

    control = pd.read_csv(control_path, keep_default_na=False)
    assert list(control.columns) == [
        "asset", "timeframe", "requested_at", "bars_requested", "status", "input_timestamp"
    ]
    assert control.loc[0, "timeframe"] == ""
    assert control.loc[1, "timeframe"] == "1D"
