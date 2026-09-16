from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from threading import Lock, RLock
from collections import OrderedDict
from copy import deepcopy

import pandas as pd

from .config import Settings
from .provider import split_asset
from .metadata import compact_coverage, expand_coverage, prune_receipts


_WRITE_LOCK = RLock()
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
        self._coverage_cache = OrderedDict()

    def _coverage_document(self, path):
        """Private document, accessed under the storage lock; never returned directly."""
        stamp = (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
        cached = self._coverage_cache.get(path)
        if cached is None or cached[0] != stamp:
            cached = (stamp, read_json(path, {}))
            self._coverage_cache[path] = cached
        self._coverage_cache.move_to_end(path)
        while len(self._coverage_cache) > 16:
            self._coverage_cache.popitem(last=False)
        return cached[1]

    def _write_coverage_document(self, path, document):
        atomic_json(path, document)
        self._coverage_cache[path] = ((path.stat().st_mtime_ns, path.stat().st_size), document)
        self._coverage_cache.move_to_end(path)
        while len(self._coverage_cache) > 16:
            self._coverage_cache.popitem(last=False)

    def read_coverage(self, asset):
        with _WRITE_LOCK:
            document = self._coverage_document(self.path_for(asset).parent / "coverage.json")
            return expand_coverage({k: v for k, v in document.items() if k != "receipt_evidence"})

    def save_coverage(self, asset, coverage):
        with _WRITE_LOCK:
            path = self.path_for(asset).parent / "coverage.json"
            current = self._coverage_document(path)
            document = compact_coverage({k: v for k, v in coverage.items() if k != "receipt_evidence"})
            if "metadata_schema" in current:
                document["metadata_schema"] = current["metadata_schema"]
            if "receipt_evidence" in current:
                document["receipt_evidence"] = current["receipt_evidence"]
            if document != current:
                self._write_coverage_document(path, deepcopy(document))

    def migrate_metadata(self, asset):
        with _WRITE_LOCK:
            folder = self.path_for(asset).parent
            legacy = [(tf, folder / f"{tf}.receipts.json") for tf in SUPPORTED_TIMEFRAMES]
            legacy = [(tf, path) for tf, path in legacy if path.exists()]
            if legacy:
                path = folder / "coverage.json"
                document = deepcopy(self._coverage_document(path))
                evidence = document.setdefault("receipt_evidence", {})
                for tf, old_path in legacy:
                    receipts = evidence.setdefault(tf, {})
                    for stamp, receipt in read_json(old_path, {}).items():
                        previous = receipts.get(stamp)
                        if previous is None or pd.Timestamp(receipt["request_started_at"]) > pd.Timestamp(previous["request_started_at"]):
                            receipts[stamp] = receipt
                self._write_coverage_document(path, document)
                # Delete only after the complete consolidated document is durable.
                for _, old_path in legacy:
                    old_path.unlink()
            self._migrate_revision_logs(folder)
            path = folder / "coverage.json"
            current = self._coverage_document(path)
            calendar = read_json(folder / "calendar.json", {})
            if calendar and current.get("metadata_schema") != 2:
                document = compact_coverage(expand_coverage(current))
                for tf, receipts in document.get("receipt_evidence", {}).items():
                    prices = self.read(asset, tf)
                    document["receipt_evidence"][tf] = prune_receipts(receipts, prices.index, calendar)
                document["metadata_schema"] = 2
                self._write_coverage_document(path, document)

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
        with _WRITE_LOCK:
            document = self._coverage_document(path.parent / "coverage.json")
            receipts = document.get("receipt_evidence", {}).get(timeframe)
            result.attrs["receipts"] = deepcopy(receipts) if receipts is not None else read_json(path.with_suffix(".receipts.json"), {})
        return result

    def merge_and_write(
        self,
        asset: str,
        fresh: pd.DataFrame,
        timeframe: str = "1h",
        received_after: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        with _WRITE_LOCK:
            self.migrate_metadata(asset)
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
                    # Updating the previous tip, including its final refresh when
                    # a successor arrives, is ordinary candle formation.
                    if stamp == current.index[-1]:
                        continue
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
                    with (path.parent / "revisions.jsonl").open("a", encoding="utf-8") as handle:
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
                receipts = prune_receipts(receipts, merged.index, read_json(path.parent / "calendar.json", {}))
                coverage_path = path.parent / "coverage.json"
                document = dict(self._coverage_document(coverage_path))
                evidence = dict(document.get("receipt_evidence", {}))
                evidence[timeframe] = deepcopy(receipts)
                document["receipt_evidence"] = evidence
                self._write_coverage_document(coverage_path, document)
            merged.attrs["receipts"] = receipts
            return merged

    @staticmethod
    def _migrate_revision_logs(folder: Path) -> None:
        """Consolidate legacy logs before removing them; caller holds write lock."""
        legacy = [folder / f"{tf}.revisions.jsonl" for tf in SUPPORTED_TIMEFRAMES]
        legacy = [path for path in legacy if path.exists()]
        if not legacy:
            return
        target = folder / "revisions.jsonl"
        records = {}
        for path in ([target] if target.exists() else []) + legacy:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if path != target:
                    record.setdefault("timeframe", path.name.split(".")[0])
                records[json.dumps(record, sort_keys=True)] = record
        fd, name = tempfile.mkstemp(prefix="revisions-", suffix=".tmp", dir=folder)
        os.close(fd)
        temporary = Path(name)
        try:
            temporary.write_text("".join(json.dumps(record) + "\n" for record in
                                 sorted(records.values(), key=lambda r: r.get("observed_at", ""))),
                                 encoding="utf-8")
            os.replace(temporary, target)
            for path in legacy:
                path.unlink()
        finally:
            temporary.unlink(missing_ok=True)

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
