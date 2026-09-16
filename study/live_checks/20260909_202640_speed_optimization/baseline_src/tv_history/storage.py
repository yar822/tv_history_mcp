from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from threading import Lock

import pandas as pd

from .config import Settings
from .provider import split_asset


_WRITE_LOCK = Lock()
_CONTROL_LOCK = Lock()
SUPPORTED_TIMEFRAMES = ("1h", "4h", "1D", "1W")
CONTROL_FIELDS = (
    "asset",
    "timeframe",
    "requested_at",
    "bars_requested",
    "status",
    "input_timestamp",
)


class CsvStorage:
    def __init__(self, settings: Settings):
        self.settings = settings

    def path_for(self, asset: str, timeframe: str = "1h") -> Path:
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError("storage timeframe must be one of: 1h, 4h, 1D, 1W")
        symbol, exchange = split_asset(asset)
        safe_exchange = storage_segment(exchange)
        safe_symbol = storage_segment(symbol)
        filename = f"{timeframe}.csv"
        candidate = self.settings.data_root / safe_exchange / safe_symbol / filename

        # Reuse folders created by earlier versions, notably NQ1! -> NQ1_.
        legacy_symbol = re.sub(r"[^A-Z0-9._-]", "_", symbol)
        legacy = self.settings.data_root / exchange / legacy_symbol / filename
        if exchange == safe_exchange and legacy.exists():
            candidate = legacy

        root = self.settings.data_root.resolve()
        resolved = candidate.resolve()
        if root != resolved and root not in resolved.parents:
            raise ValueError("asset storage path must remain under the configured data root")
        return candidate

    def read(self, asset: str, timeframe: str = "1h") -> pd.DataFrame:
        path = self.path_for(asset, timeframe)
        if not path.exists():
            return empty_frame()
        frame = pd.read_csv(path, parse_dates=["timestamp_utc"])
        frame = frame.set_index("timestamp_utc")
        frame.index = pd.DatetimeIndex(frame.index).tz_convert("UTC")
        result = frame[["open", "high", "low", "close", "volume"]].astype(float).sort_index()
        result.attrs["receipts"] = read_json(path.with_suffix(".receipts.json"), {})
        return result

    def merge_and_write(
        self,
        asset: str,
        fresh: pd.DataFrame,
        timeframe: str = "1h",
        received_after: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        with _WRITE_LOCK:
            current = self.read(asset, timeframe)
            receipts = dict(current.attrs.get("receipts", {}))
            # Pandas propagates attrs into every .loc/iterrows result. Full-batch
            # refreshes must not deepcopy thousands of receipt records per row.
            current.attrs = {}
            path = self.path_for(asset, timeframe)
            if received_after is not None:
                observed = pd.Timestamp.now(tz="UTC").isoformat()
                changes = []
                for stamp in current.index.intersection(fresh.index):
                    before, after = current.loc[stamp], fresh.loc[stamp]
                    fields = [name for name in ("open", "high", "low", "close", "volume")
                              if float(before[name]) != float(after[name])]
                    if fields:
                        changes.append({"asset": asset, "timeframe": timeframe,
                                        "bar_open": stamp.isoformat(), "observed_at": observed,
                                        "before": {k: float(before[k]) for k in fields},
                                        "after": {k: float(after[k]) for k in fields}})
                if changes:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.with_suffix(".revisions.jsonl").open("a", encoding="utf-8") as handle:
                        for change in changes:
                            handle.write(json.dumps(change) + "\n")
                for stamp, row in fresh.iterrows():
                    receipts[stamp.isoformat()] = {"request_started_at": received_after.isoformat(),
                                                  "received_at": observed,
                                                  "ohlcv": [float(row[k]) for k in ("open", "high", "low", "close", "volume")]}
            # Frame attrs contain dictionaries with receipts; pandas concat must
            # not compare these metadata payloads while merging rows.
            current.attrs = {}
            fresh = fresh.copy()
            fresh.attrs = {}
            merged = pd.concat([current, fresh])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            validate_frame(merged)
            self._atomic_write(self.path_for(asset, timeframe), merged)
            if received_after is not None:
                atomic_json(path.with_suffix(".receipts.json"), receipts)
            merged.attrs["receipts"] = receipts
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


class DownloadControlLog:
    def __init__(self, data_root: Path):
        self.path = data_root / "download_control.csv"
        self.pending_path = data_root / "download_control.pending.csv"
        with _CONTROL_LOCK:
            try:
                self._migrate_schema()
                self._flush_pending()
            except PermissionError:
                # A CSV viewer such as Excel may hold the control file open.
                # Downloads are queued and merged after that lock is released.
                pass

    def record(
        self,
        asset: str,
        timeframe: str,
        requested_at: pd.Timestamp,
        bars_requested: int,
        status: str,
    ) -> None:
        row = {
            "asset": asset,
            "timeframe": timeframe,
            "requested_at": requested_at.isoformat(),
            "bars_requested": int(bars_requested),
            "status": status,
            "input_timestamp": pd.Timestamp.now(tz="UTC").isoformat(),
        }
        with _CONTROL_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._migrate_schema()
                self._flush_pending()
                self._append_row(self.path, row)
            except PermissionError:
                self._append_row(self.pending_path, row)

    @staticmethod
    def _append_row(path: Path, row: dict) -> None:
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CONTROL_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

    def _flush_pending(self) -> None:
        if not self.pending_path.exists() or self.pending_path.stat().st_size == 0:
            return
        with self.pending_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            self._append_row(self.path, row)
        self.pending_path.unlink()

    def _migrate_schema(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self.path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames == list(CONTROL_FIELDS):
                return
            rows = list(reader)
        fd, temp_name = tempfile.mkstemp(
            prefix="download_control-", suffix=".tmp", dir=self.path.parent
        )
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            self._write_migrated_rows(temp_path, rows)
            try:
                os.replace(temp_path, self.path)
            except PermissionError:
                # Windows cannot replace a file held open by another process,
                # even when that process permits writes to the file itself.
                self._write_migrated_rows(self.path, rows)
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _write_migrated_rows(path: Path, rows: list[dict[str, str]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CONTROL_FIELDS)
            writer.writeheader()
            for existing in rows:
                writer.writerow(
                    {field: existing.get(field, "") for field in CONTROL_FIELDS}
                )


def storage_segment(value: str) -> str:
    readable = re.sub(r"[^A-Z0-9._-]", "_", value).strip(" .") or "asset"
    if readable == value and value not in {".", ".."}:
        return readable
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{readable[:80]}-{digest}"


def empty_frame() -> pd.DataFrame:
    frame = pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
    frame.index = pd.DatetimeIndex([], tz="UTC", name="timestamp_utc")
    return frame


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.stem, suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp = Path(name)
    try:
        temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


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
