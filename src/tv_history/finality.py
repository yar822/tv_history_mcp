from __future__ import annotations

import pandas as pd

from .config import Settings
from .provider import split_asset


def finalization_delay(settings: Settings, asset: str) -> pd.Timedelta:
    _symbol, exchange = split_asset(asset)
    overrides = settings.finalization_delay_by_exchange or {}
    minutes = overrides.get(exchange, settings.finalization_delay_minutes)
    return pd.Timedelta(minutes=int(minutes))


def finality_flags(
    opened: pd.DataFrame,
    duration: pd.Timedelta,
    evaluated_at: pd.Timestamp,
    delay: pd.Timedelta,
) -> pd.Series:
    flags = pd.Series(False, index=opened.index, dtype=bool)
    if opened.empty:
        return flags
    if len(opened) > 1:
        flags.iloc[:-1] = True
    flags.iloc[-1] = bool(evaluated_at >= opened.index[-1] + duration + delay)
    return flags
