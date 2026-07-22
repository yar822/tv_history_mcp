from __future__ import annotations

import pandas as pd

from .analysis import parse_timestamp, timeframe_duration
from .config import Settings
from .errors import error_response, is_transient_error
from .execution import data_quality, gap_overlaps_weekend
from .indicators import calculate_indicators
from .provider import normalize_asset
from .resample import completed_as_of, iso_utc
from .sync import HistorySynchronizer
from .windows import select_sessions, sessions_covered


MAX_COUNT = 1000


class AssetBarsService:
    def __init__(self, settings: Settings, synchronizer: HistorySynchronizer):
        self.settings = settings
        self.synchronizer = synchronizer

    def get_bars(
        self,
        asset: str,
        timeframe: str,
        timestamp: str | None,
        count: int = 100,
        sessions: int | None = None,
    ) -> dict:
        try:
            normalized_asset = normalize_asset(asset)
            requested_at = parse_timestamp(timestamp)
            duration = timeframe_duration(timeframe)
            parsed_sessions = parse_positive_integer(sessions, "sessions") if sessions is not None else None
            parsed_count = parse_count(count) if parsed_sessions is None else count
        except (TypeError, ValueError) as exc:
            return error_response("INVALID_PARAMETER", str(exc))

        try:
            source, _refresh = self.synchronizer.ensure_available(
                normalized_asset, timeframe, requested_at
            )
            completed = completed_as_of(source, timeframe, requested_at)
            bars = (
                select_sessions(completed, parsed_sessions, normalized_asset)
                if parsed_sessions is not None
                else completed.tail(parsed_count)
            )
            atr = None
            if not bars.empty:
                atr = calculate_indicators(bars, self.settings.indicators).iloc[-1].get("ATR")
            quality = data_quality(bars, timeframe, normalized_asset)
            scheduled_gaps = quality["scheduled_session_gaps"]
            if (
                not bars.empty
                and bars.index[-1] + duration < requested_at
                and gap_overlaps_weekend(bars.index[-1] + duration, requested_at)
            ):
                scheduled_gaps += 1
            return {
                "asset": normalized_asset,
                "timeframe": timeframe,
                "requested_at": requested_at.isoformat(),
                "effective_bar_close": (
                    (bars.index[-1] + duration).isoformat() if not bars.empty else None
                ),
                "bars": [serialize_bar(index, row) for index, row in bars.iterrows()],
                "sessions_covered": sessions_covered(bars, normalized_asset),
                "bars_returned": len(bars),
                "atr_14": finite_number(atr),
                "data_quality": {
                    "unexpected_missing_bars": bool(quality["unexpected_missing_bars"]),
                    "scheduled_session_gaps": scheduled_gaps,
                },
            }
        except Exception as exc:
            code = "ASSET_NOT_FOUND" if "returned no data" in str(exc) else "DATA_PROVIDER_ERROR"
            return error_response(code, str(exc), retryable=is_transient_error(exc))


def parse_count(value: int) -> int:
    parsed = parse_positive_integer(value, "count")
    if parsed > MAX_COUNT:
        raise ValueError(f"count must be between 1 and {MAX_COUNT}")
    return parsed


def parse_positive_integer(value, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if str(value).strip() != str(parsed) or parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def serialize_bar(index: pd.Timestamp, row: pd.Series) -> dict:
    return {
        "t": iso_utc(index),
        "o": float(row["open"]),
        "h": float(row["high"]),
        "l": float(row["low"]),
        "c": float(row["close"]),
        "v": float(row["volume"]),
    }


def finite_number(value):
    return round(float(value), 2) if value is not None and not pd.isna(value) else None
