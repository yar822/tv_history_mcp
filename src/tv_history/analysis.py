from __future__ import annotations

import math

import pandas as pd

from .config import Settings
from .resample import completed_as_of, resample_ohlcv
from .storage import CsvStorage
from .sync import HistorySynchronizer


class AssetAnalysisService:
    def __init__(self, settings: Settings, synchronizer: HistorySynchronizer, storage: CsvStorage):
        self.settings = settings
        self.synchronizer = synchronizer
        self.storage = storage

    def analyze(self, asset: str, timeframe: str, timestamp: str | None) -> dict:
        normalized_asset = asset.strip().upper()
        if normalized_asset not in self.settings.assets:
            return {
                "error": "asset_not_configured",
                "asset": normalized_asset,
                "configured_assets": sorted(self.settings.assets),
            }
        try:
            requested_at = parse_timestamp(timestamp)
            timeframe_duration(timeframe)
            hourly, refresh = self.synchronizer.ensure_available(normalized_asset, requested_at)
            resampled = resample_ohlcv(hourly, timeframe)
            closed = completed_as_of(resampled, timeframe, requested_at)
            if closed.empty:
                return {
                    "error": "timestamp_not_covered",
                    "asset": normalized_asset,
                    "timeframe": timeframe,
                    "requested_at": requested_at.isoformat(),
                    "available_from": hourly.index.min().isoformat() if not hourly.empty else None,
                    "refresh": refresh,
                }

            from .indicators import calculate_indicators

            calculated = calculate_indicators(closed, self.settings.indicators)
            row = calculated.iloc[-1]
            bar_open = calculated.index[-1]
            price_change = percent_change(row["open"], row["close"])
            volume_ratio = safe_ratio(row["volume"], row["volume.SMA20"])
            bb_position = (
                "ABOVE" if valid(row["BB.upper"]) and row["close"] > row["BB.upper"]
                else "BELOW" if valid(row["BB.lower"]) and row["close"] < row["BB.lower"]
                else "WITHIN"
            )
            trend = trend_label(row)
            return {
                "asset": normalized_asset,
                "timeframe": timeframe,
                "requested_at": requested_at.isoformat(),
                "effective_bar_open": bar_open.isoformat(),
                "effective_bar_close": (bar_open + timeframe_duration(timeframe)).isoformat(),
                "source": "tvdatafeed_local_csv",
                "refresh": refresh,
                "storage": {
                    "path": str(self.storage.path_for(normalized_asset)),
                    "hourly_rows": len(hourly),
                    "available_from": hourly.index.min().isoformat(),
                    "available_to": hourly.index.max().isoformat(),
                },
                "price_data": {
                    "open": number(row["open"]),
                    "high": number(row["high"]),
                    "low": number(row["low"]),
                    "close": number(row["close"]),
                    "change_percent": number(price_change),
                    "volume": number(row["volume"]),
                },
                "rsi": {
                    "value": number(row["RSI"]),
                    "signal": rsi_label(row["RSI"]),
                },
                "macd": {
                    "macd_line": number(row["MACD.macd"]),
                    "signal_line": number(row["MACD.signal"]),
                    "crossover": crossover_label(row["MACD.macd"], row["MACD.signal"]),
                },
                "sma": values(row, "SMA", self.settings.indicators["sma"]),
                "ema": values(row, "EMA", self.settings.indicators["ema"]),
                "bollinger_bands": {
                    "upper": number(row["BB.upper"]),
                    "middle": number(row["BB.middle"]),
                    "lower": number(row["BB.lower"]),
                    "position": bb_position,
                },
                "atr": {"value": number(row["ATR"])},
                "volume_analysis": {
                    "average_20": number(row["volume.SMA20"]),
                    "ratio": number(volume_ratio),
                    "signal": volume_label(volume_ratio),
                },
                "market_sentiment": {
                    "trend": trend,
                    "momentum": "Bullish" if price_change > 0 else "Bearish" if price_change < 0 else "Flat",
                },
            }
        except ValueError as exc:
            return {"error": "invalid_parameter", "message": str(exc)}
        except Exception as exc:
            return {"error": "analysis_failed", "message": str(exc), "asset": normalized_asset}


def parse_timestamp(value: str | None) -> pd.Timestamp:
    timestamp = pd.Timestamp.now(tz="UTC") if not value else pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def timeframe_duration(timeframe: str) -> pd.Timedelta:
    durations = {"1h": "1h", "4h": "4h", "1D": "1D", "1W": "7D"}
    if timeframe not in durations:
        raise ValueError("timeframe must be one of: 1h, 4h, 1D, 1W")
    return pd.Timedelta(durations[timeframe])


def valid(value) -> bool:
    return value is not None and not pd.isna(value) and math.isfinite(float(value))


def number(value, digits: int = 6):
    return round(float(value), digits) if valid(value) else None


def safe_ratio(numerator, denominator) -> float | None:
    return float(numerator / denominator) if valid(denominator) and denominator != 0 else None


def percent_change(open_price, close_price) -> float:
    return float((close_price - open_price) / open_price * 100) if open_price else 0.0


def values(row, prefix: str, periods: list[int]) -> dict:
    return {f"{prefix.lower()}{period}": number(row[f"{prefix}{period}"]) for period in periods}


def rsi_label(value) -> str:
    if not valid(value): return "Unavailable"
    if value > 70: return "Overbought"
    if value < 30: return "Oversold"
    if value > 60: return "Bullish"
    if value < 40: return "Bearish"
    return "Neutral"


def crossover_label(macd, signal) -> str:
    if not valid(macd) or not valid(signal): return "Unavailable"
    return "Bullish" if macd > signal else "Bearish" if macd < signal else "Neutral"


def volume_label(ratio) -> str:
    if ratio is None: return "Unavailable"
    if ratio >= 2: return "High"
    if ratio >= 1.5: return "Above Average"
    if ratio < 0.8: return "Below Average"
    return "Normal"


def trend_label(row) -> str:
    close, ema20, ema50 = row["close"], row["EMA20"], row["EMA50"]
    if not valid(ema20) or not valid(ema50): return "Unavailable"
    if close > ema20 > ema50: return "Bullish"
    if close < ema20 < ema50: return "Bearish"
    return "Mixed"
