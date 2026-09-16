from __future__ import annotations

import pandas as pd

def select_sessions(frame: pd.DataFrame, sessions: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    trading_dates = pd.Index(frame.index.date)
    available_dates = trading_dates.drop_duplicates()
    selected_dates = set(available_dates[-sessions:])
    return frame.loc[trading_dates.isin(selected_dates)]


def sessions_covered(frame: pd.DataFrame) -> int:
    return len(pd.Index(frame.index.date).drop_duplicates()) if not frame.empty else 0
