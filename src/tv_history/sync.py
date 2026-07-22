from __future__ import annotations

import math
import time

import pandas as pd

from .config import Settings
from .provider import HistoryProvider
from .storage import CsvStorage, DownloadControlLog


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
                return stored, {
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
        return merged, {
            "refreshed": True,
            "reason": reason,
            "bars_requested": request_bars,
            "bars_received": len(fresh),
        }

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
