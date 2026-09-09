from __future__ import annotations

import math
import time

import pandas as pd

from .config import Settings
from .provider import HistoryProvider
from .storage import CsvStorage, DownloadControlLog
from .trading_dates import normalize_daily, normalize_weekly, uses_trading_dates


TIMEFRAME_DURATIONS = {
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "1D": pd.Timedelta(days=1),
    "1W": pd.Timedelta(days=7),
}


class HistorySynchronizer:
    def __init__(self, settings: Settings, provider: HistoryProvider, storage: CsvStorage):
        self.settings = settings
        self.provider = provider
        self.storage = storage
        self.control_log = DownloadControlLog(settings.data_root)

    def ensure_available(
        self, asset: str, timeframe: str, requested_at: pd.Timestamp
    ) -> tuple[pd.DataFrame, dict]:
        if timeframe not in TIMEFRAME_DURATIONS:
            raise ValueError("timeframe must be one of: 1h, 4h, 1D, 1W")
        stored = self.storage.read(asset, timeframe)
        if stored.empty:
            request_bars = self.settings.initial_bars
            reason = "initial_load"
        else:
            last_stored = stored.index.max()
            if requested_at <= last_stored:
                return self._served_view(asset, timeframe, stored, requested_at), {
                    "refreshed": False,
                    "reason": "requested_timestamp_is_covered",
                    "bars_requested": 0,
                }
            missing_bars = max(
                0, math.ceil((requested_at - last_stored) / TIMEFRAME_DURATIONS[timeframe])
            )
            request_bars = min(
                self.settings.initial_bars,
                missing_bars + self.settings.refresh_overlap_bars,
            )
            reason = "requested_timestamp_is_later_than_storage"

        try:
            fresh = self._download_with_retries(asset, timeframe, request_bars)
            merged = self.storage.merge_and_write(asset, fresh, timeframe)
        except Exception:
            self.control_log.record(asset, timeframe, requested_at, request_bars, "failure")
            raise
        self.control_log.record(asset, timeframe, requested_at, request_bars, "success")
        return self._served_view(asset, timeframe, merged, requested_at), {
            "refreshed": True,
            "reason": reason,
            "bars_requested": request_bars,
            "bars_received": len(fresh),
        }

    def _served_view(self, asset, timeframe, source, requested_at):
        if self.settings.timestamp_profiles is not None and timeframe in {"1D", "1W"}:
            from .provider import split_asset
            from .timestamp_profiles import served_profile
            symbol, exchange = split_asset(asset)
            profile = self.settings.timestamp_profiles.get(f"{exchange}:{symbol}")
            if profile is None:
                return source
            return served_profile(
                source, source if timeframe == "1D" else self.storage.read(asset, "1D"),
                self.storage.read(asset, "4h"), self.storage.read(asset, "1h"),
                min(requested_at, pd.Timestamp.now(tz="UTC")), timeframe, profile,
            )
        if not uses_trading_dates(asset, timeframe, self.settings.rus_daily_trading_date_assets):
            return source
        if timeframe == "1W":
            return normalize_weekly(
                source,
                self.storage.read(asset, "1D"),
                self.storage.read(asset, "4h"),
                min(requested_at, pd.Timestamp.now(tz="UTC")),
                hourly=self.storage.read(asset, "1h"),
            )
        return normalize_daily(
            source,
            self.storage.read(asset, "4h"),
            min(requested_at, pd.Timestamp.now(tz="UTC")),
            hourly=self.storage.read(asset, "1h"),
        )

    def _download_with_retries(
        self, asset: str, timeframe: str, request_bars: int
    ) -> pd.DataFrame:
        attempt_sizes = (
            min(request_bars, 5000),
            min(request_bars, 5000),
            min(request_bars, 5000),
            min(request_bars, 4000),
            min(request_bars, 2000),
        )
        for attempt, bars in enumerate(attempt_sizes, start=1):
            try:
                return self.provider.get_history(asset, timeframe, bars)
            except Exception:
                if attempt == len(attempt_sizes):
                    raise
                time.sleep(5)
        raise RuntimeError("unreachable")
