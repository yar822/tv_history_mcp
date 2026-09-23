from __future__ import annotations

import math

import pandas as pd

from .config import Settings
from .errors import error_response, is_transient_error
from .finality import finality_flags, finalization_delay, completion_details, calendar_metadata
from .provider import normalize_asset
from .resample import completed_as_of
from .sync import HistorySynchronizer
from .windows import select_sessions, sessions_covered
from .coverage import window_coverage


class AssetAnalysisService:
    def __init__(self, settings: Settings, synchronizer: HistorySynchronizer):
        self.settings = settings
        self.synchronizer = synchronizer

    def analyze(
        self,
        asset: str,
        timeframe: str,
        timestamp: str | None,
        response_version: str = "execution",
        include_indicators: bool = False,
    ) -> dict:
        try:
            live_request = timestamp is None
            normalized_asset = normalize_asset(asset)
            requested_at = parse_timestamp(timestamp)
            duration = timeframe_duration(timeframe)
            if response_version != "execution":
                raise ValueError("response_version must be execution")
            source, _refresh = self.synchronizer.ensure_available(
                normalized_asset, timeframe, requested_at, include_coverage_metadata=False
            )
            evaluated_at = min(requested_at, current_utc_time())
            coverage = window_coverage(source, evaluated_at)
            if coverage["status"] == "incomplete":
                return error_response("INSUFFICIENT_HISTORY_COVERAGE",
                                      "Required analysis history contains missing or unavailable bars.")
            opened = source.loc[source.index <= evaluated_at]
            details = completion_details(opened, duration, evaluated_at,
                                         finalization_delay(self.settings, normalized_asset))
            # Completion evidence is consumed above and retained on source for
            # response metadata. Price/indicator row loops must not copy the
            # full receipt map and historical gap list thousands of times.
            opened.attrs = {}
            closed = opened.loc[details["is_bar_complete"]].copy()
            closed.attrs["finalized_asof"] = evaluated_at.isoformat()
            displayed = opened if live_request else closed
            price_bar_open = displayed.index[-1] if not displayed.empty else None
            price_row = displayed.iloc[-1] if not displayed.empty else None
            price_is_final = bool(details.iloc[-1]["is_bar_complete"]) if live_request and not details.empty else True
            if closed.empty:
                return execution_error(
                    "INSUFFICIENT_DATA",
                    "No completed bars are available at or before the requested timestamp.",
                    bars_available=0,
                )

            minimum_bars = 80 if timeframe == "1D" else 50
            if len(closed) < minimum_bars:
                return execution_error(
                    "INSUFFICIENT_DATA",
                    f"Execution analysis requires at least {minimum_bars} completed "
                    f"{timeframe} bars; {len(closed)} are available.",
                    bars_available=len(closed),
                )

            from .indicators import calculate_indicators

            calculated = calculate_indicators(closed, self.settings.indicators)
            daily_source = closed
            if timeframe != "1D":
                daily_source, _daily_refresh = self.synchronizer.ensure_available(
                    normalized_asset, "1D", requested_at, include_coverage_metadata=False
                )
                daily_coverage = window_coverage(daily_source, evaluated_at)
                if daily_coverage["status"] == "incomplete":
                    return error_response("INSUFFICIENT_HISTORY_COVERAGE",
                                          "Required daily analysis history contains missing or unavailable bars.")
                daily_opened = daily_source.loc[daily_source.index <= evaluated_at]
                daily_flags = finality_flags(daily_opened, timeframe_duration("1D"), evaluated_at,
                                            finalization_delay(self.settings, normalized_asset))
                daily_opened.attrs = {}
                daily_source = daily_opened.loc[daily_flags].copy()
                daily_source.attrs["finalized_asof"] = evaluated_at.isoformat()
            from .execution import build_execution_response

            result = build_execution_response(
                normalized_asset,
                timeframe,
                requested_at,
                daily_source,
                closed,
                calculated,
                self.settings.indicators,
                include_indicators,
                price_row,
                price_bar_open,
                price_is_final,
            )
            result["completion_calendar"] = calendar_metadata(source)
            from .execution import data_quality
            result["data_quality"] = data_quality(closed, timeframe, normalized_asset,
                                                  source.attrs.get("completion_context", {}).get("calendar"))
            shown_details = details if live_request else details.loc[details["is_bar_complete"]]
            result["completion_reason"] = shown_details.iloc[-1]["completion_reason"]
            result["completion_boundary"] = shown_details.iloc[-1]["completion_boundary"]
            return result
        except ValueError as exc:
            message = str(exc)
            if "timeframe must be" in message:
                return error_response("UNSUPPORTED_TIMEFRAME", message)
            if message.startswith("asset "):
                return error_response("INVALID_PARAMETER", message)
            if "ambiguous time" in message:
                return error_response("DATA_PROVIDER_ERROR", message, retryable=False)
            return error_response("INVALID_PARAMETER", message)
        except Exception as exc:
            message = str(exc)
            if "returned no data" in message:
                return error_response("ASSET_NOT_FOUND", message)
            return error_response(
                "DATA_PROVIDER_ERROR", message, retryable=is_transient_error(exc)
            )


def execution_error(
    code: str,
    message: str,
    retryable: bool = False,
    bars_available: int | None = None,
) -> dict:
    details = {"bars_available": bars_available} if bars_available is not None else {}
    return error_response(code, message, retryable=retryable, **details)


def parse_timestamp(value: str | None) -> pd.Timestamp:
    timestamp = pd.Timestamp.now(tz="UTC") if not value else pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def current_utc_time() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def timeframe_duration(timeframe: str) -> pd.Timedelta:
    durations = {"1h": "1h", "4h": "4h", "1D": "1D", "1W": "7D"}
    if timeframe not in durations:
        raise ValueError("timeframe must be one of: 1h, 4h, 1D, 1W")
    return pd.Timedelta(durations[timeframe])


def valid(value) -> bool:
    return value is not None and not pd.isna(value) and math.isfinite(float(value))


def number(value, digits: int = 2):
    return round(float(value), digits) if valid(value) else None


def safe_ratio(numerator, denominator) -> float | None:
    return float(numerator / denominator) if valid(denominator) and denominator != 0 else None


def percent_change(open_price, close_price) -> float:
    return float((close_price - open_price) / open_price * 100) if open_price else 0.0


def short_term_metrics(
    frame: pd.DataFrame,
    requested_sessions: int,
    row_atr,
    sharp_move_bars: int = 6,
) -> dict:
    window = select_sessions(frame, requested_sessions)
    closes = window["close"]
    atr = float(row_atr) if valid(row_atr) and float(row_atr) != 0 else None
    path_length = float(closes.diff().abs().sum()) if len(closes) >= 2 else 0.0
    efficiency = (
        abs(float(closes.iloc[-1] - closes.iloc[0])) / path_length
        if path_length > 0 else None
    )
    sharp_move = None
    if len(closes) > sharp_move_bars:
        changes = closes.diff(sharp_move_bars).dropna()
        if not changes.empty:
            sharp_move = float(changes.loc[changes.abs().idxmax()])
    return {
        "sessions_covered": sessions_covered(window),
        "bars_used": len(window),
        "efficiency_ratio": number(efficiency),
        "span_atr": number(
            (float(window["high"].max()) - float(window["low"].min())) / atr
            if atr is not None and not window.empty else None
        ),
        "net_atr": number(
            float(closes.iloc[-1] - closes.iloc[0]) / atr
            if atr is not None and not closes.empty else None
        ),
        "sharp_move_atr": number(sharp_move / atr if atr is not None and sharp_move is not None else None),
        "sharp_move_bars": sharp_move_bars,
    }


def values(row, prefix: str, periods: list[int]) -> dict:
    return {f"{prefix.lower()}{period}": number(row[f"{prefix}{period}"]) for period in periods}


def direction_label(current, previous) -> str:
    if not valid(current) or not valid(previous):
        return "Unavailable"
    return "Rising" if current > previous else "Falling" if current < previous else "Flat"


def moving_average_signals(row, prefix: str) -> list[str]:
    close = row["close"]
    signals: list[str] = []
    for period, horizon in ((20, "short-term"), (50, "mid-term"), (200, "long-term")):
        value = row.get(f"{prefix}{period}")
        if valid(value):
            above = close > value
            signals.append(
                f"Price {'above' if above else 'below'} {prefix}{period} "
                f"({horizon} {'bullish' if above else 'bearish'})"
            )
    fast = row.get(f"{prefix}50")
    slow = row.get(f"{prefix}200")
    if valid(fast) and valid(slow):
        signals.append(
            f"Golden Cross ({prefix}50 > {prefix}200)"
            if fast > slow else f"Death Cross ({prefix}50 < {prefix}200)"
        )
    return signals


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


def adx_analysis(row) -> dict:
    adx, plus_di, minus_di = row.get("ADX"), row.get("ADX+DI"), row.get("ADX-DI")
    strength = (
        "Unavailable" if not valid(adx) else
        "Strong Trend" if adx > 25 else
        "Weak/No Trend" if adx < 20 else "Moderate"
    )
    di_signal = (
        "Unavailable" if not valid(plus_di) or not valid(minus_di) else
        "Bullish (+DI > -DI)" if plus_di > minus_di else
        "Bearish (-DI > +DI)" if minus_di > plus_di else "Neutral"
    )
    return {
        "value": number(adx),
        "trend_strength": strength,
        "plus_di": number(plus_di),
        "minus_di": number(minus_di),
        "di_signal": di_signal,
    }


def candle_analysis(row, timeframe: str) -> dict:
    open_price, close = float(row["open"]), float(row["close"])
    high, low = float(row["high"]), float(row["low"])
    candle_range = high - low
    body_ratio = abs(close - open_price) / candle_range if candle_range > 0 else 0.0
    body_change_pct = abs(close - open_price) / open_price * 100 if open_price else 0.0
    neutral_threshold = {"1h": 0.5, "4h": 1.0, "1D": 2.0, "1W": 2.0}[timeframe]
    upper_wick = high - max(open_price, close)
    lower_wick = min(open_price, close) - low
    return {
        "type": (
            "Neutral" if body_change_pct <= neutral_threshold else
            "Bullish" if close > open_price else
            "Bearish" if close < open_price else "Neutral"
        ),
        "body_ratio": number(body_ratio),
        "strength": "Strong" if body_ratio >= 0.7 else "Moderate" if body_ratio >= 0.4 else "Weak",
        "upper_wick_pct": number(upper_wick / candle_range * 100 if candle_range > 0 else 0),
        "lower_wick_pct": number(lower_wick / candle_range * 100 if candle_range > 0 else 0),
    }


def daily_atr_bands(daily_source: pd.DataFrame, requested_at: pd.Timestamp, config: dict) -> dict:
    from .indicators import calculate_indicators

    daily = completed_as_of(daily_source, "1D", requested_at)
    if daily.empty:
        return {"value": None, "atr_upper": None, "atr_lower": None}
    row = calculate_indicators(daily, config).iloc[-1]
    coefficient = float(config.get("atr_daily_band_coefficient", 0.4))
    atr, close = row["ATR"], row["close"]
    return {
        "value": number(atr),
        "atr_upper": number(close + coefficient * atr) if valid(atr) else None,
        "atr_lower": number(close - coefficient * atr) if valid(atr) else None,
    }


def support_resistance(
    source_frame: pd.DataFrame,
    timeframe: str,
    requested_at: pd.Timestamp,
    current_close: float,
) -> dict:
    aggregation = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if timeframe in ("1h", "4h"):
        periods = source_frame.resample("W-MON", label="left", closed="left").agg(aggregation)
        completed = periods.loc[(periods.index + pd.Timedelta(days=7)) <= requested_at]
    else:
        periods = source_frame.resample("MS", label="left", closed="left").agg(aggregation)
        completed = periods.loc[(periods.index + pd.offsets.MonthBegin(1)) <= requested_at]
    completed = completed.dropna(subset=["high", "low", "close"])
    if completed.empty:
        return empty_support_resistance()

    source = completed.iloc[-1]
    high, low, close = float(source["high"]), float(source["low"]), float(source["close"])
    price_range = high - low
    pivot = (high + low + close) / 3
    resistances = [2 * pivot - low, pivot + price_range, pivot + 2 * price_range]
    supports = [2 * pivot - high, pivot - price_range, pivot - 2 * price_range]
    above = [level for level in resistances if level > current_close]
    below = [level for level in supports if level < current_close]
    nearest_resistance = min(above) if above else None
    nearest_support = max(below) if below else None
    return {
        "pivot": number(pivot),
        "resistance_1": number(resistances[0]),
        "resistance_2": number(resistances[1]),
        "resistance_3": number(resistances[2]),
        "support_1": number(supports[0]),
        "support_2": number(supports[1]),
        "support_3": number(supports[2]),
        "nearest_resistance": number(nearest_resistance),
        "distance_to_resistance_pct": number(
            (nearest_resistance - current_close) / current_close * 100
            if nearest_resistance is not None else None
        ),
        "nearest_support": number(nearest_support),
        "distance_to_support_pct": number(
            (current_close - nearest_support) / current_close * 100
            if nearest_support is not None else None
        ),
    }


def empty_support_resistance() -> dict:
    return {
        key: None for key in (
            "pivot", "resistance_1", "resistance_2", "resistance_3",
            "support_1", "support_2", "support_3", "nearest_resistance",
            "distance_to_resistance_pct", "nearest_support", "distance_to_support_pct",
        )
    }
