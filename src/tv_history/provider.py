from __future__ import annotations

import os
import threading
import unicodedata
from typing import Protocol

import pandas as pd

from .config import Settings


KNOWN_EXCHANGES = frozenset(
    {
        "AMEX",
        "BINANCE",
        "BITSTAMP",
        "BSE",
        "BYBIT",
        "CBOE",
        "CBOT",
        "CME",
        "CME_MINI",
        "COINBASE",
        "COMEX",
        "EURONEXT",
        "FOREXCOM",
        "FX_IDC",
        "ICEEUR",
        "ICEUS",
        "KRAKEN",
        "LSE",
        "MOEX",
        "NASDAQ",
        "NSE",
        "NYMEX",
        "NYSE",
        "OANDA",
        "OKX",
        "RUS",
        "TSX",
    }
)


class HistoryProvider(Protocol):
    def get_history(self, asset: str, timeframe: str, n_bars: int) -> pd.DataFrame: ...


class TvDatafeedProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        # tvDatafeed reuses one chart session and swaps its websocket on every
        # get_hist call, so concurrent calls interleave frames from different
        # assets and one request can be answered with another asset's bars.
        # All downloads must run one at a time.
        self._download_lock = threading.Lock()

    def _get_client(self):
        if self._client is None:
            from tvDatafeed import TvDatafeed

            username = os.environ.get("TRADINGVIEW_USERNAME") or None
            password = os.environ.get("TRADINGVIEW_PASSWORD") or None
            self._client = TvDatafeed(username=username, password=password)
        return self._client

    def get_history(self, asset: str, timeframe: str, n_bars: int) -> pd.DataFrame:
        from tvDatafeed import Interval

        intervals = {
            "1h": Interval.in_1_hour,
            "4h": Interval.in_4_hour,
            "1D": Interval.in_daily,
            "1W": Interval.in_weekly,
        }
        if timeframe not in intervals:
            raise ValueError("timeframe must be one of: 1h, 4h, 1D, 1W")
        with self._download_lock:
            return self._get_history(asset, n_bars, intervals[timeframe])

    def _get_history(self, asset: str, n_bars: int, interval) -> pd.DataFrame:

        symbol, exchange = split_asset(asset)
        frame = self._get_client().get_hist(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            n_bars=n_bars,
            extended_session=False,
        )
        if frame is None or frame.empty:
            raise RuntimeError(f"tvDatafeed returned no data for {asset}")

        result = frame.copy()
        index = pd.DatetimeIndex(result.index)
        if index.tz is None:
            try:
                index = index.tz_localize(
                    self.settings.provider_naive_timezone,
                    ambiguous="infer",
                    nonexistent="shift_forward",
                )
            except ValueError as exc:
                if "ambiguous" not in str(exc).lower() and "infer dst" not in str(exc).lower():
                    raise
                # A lone fold-hour timestamp cannot be inferred. Prefer standard
                # time; if both occurrences exist, preserve their chronological order.
                ambiguous = index.duplicated(keep="last")
                index = index.tz_localize(
                    self.settings.provider_naive_timezone,
                    ambiguous=ambiguous,
                    nonexistent="shift_forward",
                )
        result.index = index.tz_convert("UTC")
        result.index.name = "timestamp_utc"
        return result[["open", "high", "low", "close", "volume"]].astype(float)


def split_asset(asset: str) -> tuple[str, str]:
    if not isinstance(asset, str):
        raise ValueError("asset must use EXCHANGE:SYMBOL or SYMBOL:EXCHANGE format")
    try:
        first, second = asset.strip().upper().rsplit(":", 1)
    except ValueError as exc:
        raise ValueError("asset must use EXCHANGE:SYMBOL or SYMBOL:EXCHANGE format") from exc
    if not first or not second:
        raise ValueError("asset must use EXCHANGE:SYMBOL or SYMBOL:EXCHANGE format")
    for component in (first, second):
        if (
            component in {".", ".."}
            or len(component) > 128
            or "/" in component
            or "\\" in component
            or any(unicodedata.category(character).startswith("C") for character in component)
        ):
            raise ValueError(
                "asset symbol and exchange cannot contain path separators, "
                "control characters, or path components"
            )
    if second in KNOWN_EXCHANGES and first not in KNOWN_EXCHANGES:
        return first, second
    if first in KNOWN_EXCHANGES:
        return second, first
    if any(character.isdigit() or character in "!." for character in first) and not any(
        character.isdigit() or character in "!." for character in second
    ):
        return first, second
    # TradingView notation is EXCHANGE:SYMBOL, so it is the default whenever
    # an otherwise valid pair is ambiguous.
    return second, first


def normalize_asset(asset: str) -> str:
    split_asset(asset)
    return asset.strip().upper()
