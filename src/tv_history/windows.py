from __future__ import annotations

import pandas as pd

from .provider import split_asset


CONTINUOUS_EXCHANGES = {"BINANCE", "BITSTAMP", "BYBIT", "COINBASE", "KRAKEN", "OKX"}


def select_sessions(frame: pd.DataFrame, sessions: int, asset: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    trading_dates = pd.Index(frame.index.date)
    valid = session_date_mask(frame, asset)
    available_dates = trading_dates[valid].drop_duplicates()
    selected_dates = set(available_dates[-sessions:])
    return frame.loc[valid & trading_dates.isin(selected_dates)]


def sessions_covered(frame: pd.DataFrame, asset: str) -> int:
    if frame.empty:
        return 0
    dates = pd.Index(frame.index.date)
    return len(dates[session_date_mask(frame, asset)].drop_duplicates())


def session_date_mask(frame: pd.DataFrame, asset: str):
    _symbol, exchange = split_asset(asset)
    if exchange in CONTINUOUS_EXCHANGES:
        return pd.Series(True, index=frame.index).to_numpy()
    return (frame.index.weekday < 5)
