from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from threading import Lock

import pandas as pd

from .config import Settings
from .provider import split_asset


_WRITE_LOCK = Lock()


class CsvStorage:
    def __init__(self, settings: Settings):
        self.settings = settings

    def path_for(self, asset: str) -> Path:
        symbol, exchange = split_asset(asset)
        safe_symbol = re.sub(r"[^A-Z0-9._-]", "_", symbol)
        return self.settings.data_root / exchange / safe_symbol / "1h.csv"

    def read(self, asset: str) -> pd.DataFrame:
        path = self.path_for(asset)
        if not path.exists():
            return empty_frame()
        frame = pd.read_csv(path, parse_dates=["timestamp_utc"])
        frame = frame.set_index("timestamp_utc")
        frame.index = pd.DatetimeIndex(frame.index).tz_convert("UTC")
        return frame[["open", "high", "low", "close", "volume"]].astype(float).sort_index()

    def merge_and_write(self, asset: str, fresh: pd.DataFrame) -> pd.DataFrame:
        with _WRITE_LOCK:
            current = self.read(asset)
            merged = pd.concat([current, fresh])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            validate_frame(merged)
            self._atomic_write(self.path_for(asset), merged)
            return merged

    @staticmethod
    def _atomic_write(path: Path, frame: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            frame.to_csv(temp_path, index_label="timestamp_utc")
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)


def empty_frame() -> pd.DataFrame:
    frame = pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
    frame.index = pd.DatetimeIndex([], tz="UTC", name="timestamp_utc")
    return frame


def validate_frame(frame: pd.DataFrame) -> None:
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError("historical timestamps must be unique and sorted")
    if frame[["open", "high", "low", "close"]].isna().any().any():
        raise ValueError("OHLC values cannot be missing")
    if (frame["high"] < frame[["open", "close"]].max(axis=1)).any():
        raise ValueError("high is below open or close")
    if (frame["low"] > frame[["open", "close"]].min(axis=1)).any():
        raise ValueError("low is above open or close")
    if (frame["volume"] < 0).any():
        raise ValueError("volume cannot be negative")
