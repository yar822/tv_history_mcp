from __future__ import annotations

import pandas as pd


def calculate_indicators(frame: pd.DataFrame, config: dict) -> pd.DataFrame:
    result = frame.copy()
    close = result["close"]

    for period in config["sma"]:
        result[f"SMA{period}"] = close.rolling(int(period), min_periods=int(period)).mean()
    for period in config["ema"]:
        result[f"EMA{period}"] = close.ewm(span=int(period), adjust=False, min_periods=int(period)).mean()

    rsi_period = int(config["rsi_period"])
    delta = close.diff()
    avg_gain = delta.clip(lower=0).ewm(alpha=1 / rsi_period, adjust=False, min_periods=rsi_period).mean()
    avg_loss = (-delta.clip(upper=0)).ewm(alpha=1 / rsi_period, adjust=False, min_periods=rsi_period).mean()
    rs = avg_gain / avg_loss
    result["RSI"] = 100 - (100 / (1 + rs))

    fast = int(config["macd_fast"])
    slow = int(config["macd_slow"])
    signal = int(config["macd_signal"])
    ema_fast = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    result["MACD.macd"] = ema_fast - ema_slow
    result["MACD.signal"] = result["MACD.macd"].ewm(
        span=signal, adjust=False, min_periods=signal
    ).mean()

    bb_period = int(config["bollinger_period"])
    bb_stddev = float(config["bollinger_stddev"])
    middle = close.rolling(bb_period, min_periods=bb_period).mean()
    deviation = close.rolling(bb_period, min_periods=bb_period).std(ddof=0)
    result["BB.middle"] = middle
    result["BB.upper"] = middle + bb_stddev * deviation
    result["BB.lower"] = middle - bb_stddev * deviation

    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - previous_close).abs(),
            (result["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_period = int(config["atr_period"])
    result["ATR"] = true_range.ewm(
        alpha=1 / atr_period, adjust=False, min_periods=atr_period
    ).mean()

    volume_period = int(config["volume_sma_period"])
    result["volume.SMA20"] = result["volume"].rolling(
        volume_period, min_periods=volume_period
    ).mean()
    return result
