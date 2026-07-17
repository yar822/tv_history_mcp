from __future__ import annotations

import os
from typing import Protocol

import pandas as pd

from .config import Settings


class HistoryProvider(Protocol):
    def get_hourly(self, asset: str, n_bars: int) -> pd.DataFrame: ...


class TvDatafeedProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None

    def _get_client(self):
        if self._client is None:
            from tvDatafeed import TvDatafeed

            username = os.environ.get("TRADINGVIEW_USERNAME") or None
            password = os.environ.get("TRADINGVIEW_PASSWORD") or None
            self._client = TvDatafeed(username=username, password=password)
        return self._client

    def get_hourly(self, asset: str, n_bars: int) -> pd.DataFrame:
        from tvDatafeed import Interval

        symbol, exchange = split_asset(asset)
        frame = self._get_client().get_hist(
            symbol=symbol,
            exchange=exchange,
            interval=Interval.in_1_hour,
            n_bars=n_bars,
            extended_session=False,
        )
        if frame is None or frame.empty:
            raise RuntimeError(f"tvDatafeed returned no data for {asset}")

        result = frame.copy()
        index = pd.DatetimeIndex(result.index)
        if index.tz is None:
            index = index.tz_localize(
                self.settings.provider_naive_timezone,
                ambiguous="infer",
                nonexistent="shift_forward",
            )
        result.index = index.tz_convert("UTC")
        result.index.name = "timestamp_utc"
        return result[["open", "high", "low", "close", "volume"]].astype(float)


def split_asset(asset: str) -> tuple[str, str]:
    try:
        symbol, exchange = asset.strip().upper().rsplit(":", 1)
    except ValueError as exc:
        raise ValueError("asset must use SYMBOL:EXCHANGE format") from exc
    if not symbol or not exchange:
        raise ValueError("asset must use SYMBOL:EXCHANGE format")
    return symbol, exchange
