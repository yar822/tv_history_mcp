from __future__ import annotations

import math

import pandas as pd

from .config import Settings
from .provider import HistoryProvider
from .storage import CsvStorage


class HistorySynchronizer:
    def __init__(self, settings: Settings, provider: HistoryProvider, storage: CsvStorage):
        self.settings = settings
        self.provider = provider
        self.storage = storage

    def ensure_available(self, asset: str, requested_at: pd.Timestamp) -> tuple[pd.DataFrame, dict]:
        stored = self.storage.read(asset)
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
            missing_hours = max(0, math.ceil((requested_at - last_stored) / pd.Timedelta(hours=1)))
            request_bars = min(
                self.settings.initial_bars,
                missing_hours + self.settings.refresh_overlap_bars,
            )
            reason = "requested_timestamp_is_later_than_storage"

        fresh = self.provider.get_hourly(asset, request_bars)
        merged = self.storage.merge_and_write(asset, fresh)
        return merged, {
            "refreshed": True,
            "reason": reason,
            "bars_requested": request_bars,
            "bars_received": len(fresh),
        }
