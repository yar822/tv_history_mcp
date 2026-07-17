from __future__ import annotations

import pandas as pd


TIMEFRAME_RULES = {
    "1h": None,
    "4h": "4h",
    "1D": "1D",
    "1W": "W-MON",
}

TIMEFRAME_DURATIONS = {
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "1D": pd.Timedelta(days=1),
    "1W": pd.Timedelta(days=7),
}


def resample_ohlcv(hourly: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if timeframe not in TIMEFRAME_RULES:
        raise ValueError("timeframe must be one of: 1h, 4h, 1D, 1W")
    rule = TIMEFRAME_RULES[timeframe]
    if rule is None:
        return hourly.copy()
    options = {"label": "left", "closed": "left"}
    if timeframe == "4h":
        options["origin"] = "epoch"
    result = hourly.resample(rule, **options).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    return result.dropna(subset=["open", "high", "low", "close"])


def completed_as_of(frame: pd.DataFrame, timeframe: str, requested_at: pd.Timestamp) -> pd.DataFrame:
    duration = TIMEFRAME_DURATIONS[timeframe]
    return frame.loc[(frame.index + duration) <= requested_at]
