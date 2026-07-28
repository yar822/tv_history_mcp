from __future__ import annotations

import math

import pandas as pd

from .indicators import calculate_indicators
from .provider import split_asset
from .resample import TIMEFRAME_DURATIONS, bar_status, completed_as_of, iso_utc


def build_execution_response(
    asset: str,
    timeframe: str,
    requested_at: pd.Timestamp,
    daily_source: pd.DataFrame,
    closed: pd.DataFrame,
    calculated: pd.DataFrame,
    config: dict,
    include_indicators: bool = False,
    price_row: pd.Series | None = None,
    price_bar_open: pd.Timestamp | None = None,
    price_is_final: bool = True,
) -> dict:
    row = calculated.iloc[-1]
    previous = calculated.iloc[-2] if len(calculated) >= 2 else None
    bar_open = calculated.index[-1]
    displayed_row = row if price_row is None else price_row
    displayed_bar_open = bar_open if price_bar_open is None else price_bar_open
    displayed_is_analysis_bar = displayed_bar_open == bar_open
    atr = finite_float(row.get("ATR"))
    close = finite_float(row.get("close"))
    bb_middle = finite_float(row.get("BB.middle"))
    bb_upper = finite_float(row.get("BB.upper"))
    bb_lower = finite_float(row.get("BB.lower"))
    bb_width = (
        (bb_upper - bb_lower) / bb_middle * 100
        if bb_upper is not None and bb_lower is not None and bb_middle not in (None, 0)
        else None
    )
    macd_key = f"macd_{int(config['macd_fast'])}_{int(config['macd_slow'])}"
    previous_close = (
        previous.get("close") if displayed_is_analysis_bar and previous is not None
        else row.get("close")
    )
    structure = market_structure(closed, atr)
    levels = actionable_levels(closed, timeframe, requested_at, atr, structure)
    signal = bar_signal(row, previous, structure, levels, atr)

    result = {
        "asset": asset,
        "timeframe": timeframe,
        "requested_at": requested_at.isoformat(),
        "effective_bar_close": (
            displayed_bar_open + TIMEFRAME_DURATIONS[timeframe]
        ).isoformat(),
        **execution_bar_status(
            timeframe, displayed_bar_open, requested_at, price_is_final
        ),
        "price_data": {
            "open": number(displayed_row.get("open")),
            "high": number(displayed_row.get("high")),
            "low": number(displayed_row.get("low")),
            "close": number(displayed_row.get("close")),
            "volume": number(displayed_row.get("volume")),
            "change_open_to_close_pct": number(
                percent_change(displayed_row.get("open"), displayed_row.get("close"))
            ),
            "change_from_previous_close_pct": number(
                percent_change(previous_close, displayed_row.get("close"))
            ),
            "opening_gap_pct": number(
                percent_change(previous_close, displayed_row.get("open"))
            ),
        },
        "volume_analysis": {
            "current": number(row.get("volume")),
            "average_20": number(row.get("volume.SMA20")),
            "ratio": number(safe_ratio(row.get("volume"), row.get("volume.SMA20"))),
        },
        "candle": candle_metrics(row),
        "previous_bar": previous_bar_metrics(
            previous if displayed_is_analysis_bar else row
        ),
        "volatility": volatility_metrics(
            row, previous, atr, close, daily_source, requested_at, timeframe, config
        ),
        "recent_path": recent_path(closed),
        "structure": structure,
        "trend_state": calculate_trend_state(closed, atr, structure),
        "levels": levels,
        "bar_signal": signal,
        "level_interaction": level_interaction(closed, structure, levels, atr),
        "week_to_date": week_to_date(closed, requested_at, timeframe),
        "weekly_vwap": weekly_vwap(closed, requested_at, atr, close),
        "data_quality": data_quality(closed, timeframe, asset),
    }
    session = session_context(closed, timeframe, asset)
    if session is not None:
        result["session"] = session
    if include_indicators:
        result.update(
            {
                "rsi": {
                    "value": number(row.get("RSI")),
                    "previous": number(previous.get("RSI")) if previous is not None else None,
                },
                macd_key: {
                    "macd_line": number(row.get("MACD.macd")),
                    "signal_line": number(row.get("MACD.signal")),
                },
                "sma": {"sma200": number(row.get("SMA200"))},
                "ema": {
                    "ema9": number(row.get("EMA9")),
                    "ema20": number(row.get("EMA20")),
                    "ema50": number(row.get("EMA50")),
                },
                "bollinger_bands": {
                    "upper": number(bb_upper),
                    "middle": number(bb_middle),
                    "lower": number(bb_lower),
                    "width_pct": number(bb_width),
                },
                "adx": {
                    "value": number(row.get("ADX")),
                    "plus_di": number(row.get("ADX+DI")),
                    "minus_di": number(row.get("ADX-DI")),
                },
                "momentum_change": momentum_change(row, previous, atr),
            }
        )
    return result


def execution_bar_status(
    timeframe: str,
    bar_open: pd.Timestamp,
    requested_at: pd.Timestamp,
    is_final: bool,
) -> dict:
    status = bar_status(timeframe, bar_open, requested_at)
    status["is_bar_complete"] = is_final
    return status


def previous_bar_metrics(row: pd.Series | None) -> dict:
    empty = {
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "change_percent": None,
        "volume_ratio": None,
        "body_ratio": None,
        "upper_wick_pct": None,
        "lower_wick_pct": None,
    }
    if row is None:
        return empty
    candle = candle_metrics(row)
    return {
        "open": number(row.get("open")),
        "high": number(row.get("high")),
        "low": number(row.get("low")),
        "close": number(row.get("close")),
        "change_percent": number(percent_change(row.get("open"), row.get("close"))),
        "volume_ratio": number(safe_ratio(row.get("volume"), row.get("volume.SMA20"))),
        **candle,
    }


def candle_metrics(row: pd.Series) -> dict:
    open_price = finite_float(row.get("open"))
    high = finite_float(row.get("high"))
    low = finite_float(row.get("low"))
    close = finite_float(row.get("close"))
    if None in (open_price, high, low, close):
        return {"body_ratio": None, "upper_wick_pct": None, "lower_wick_pct": None}
    candle_range = high - low
    if candle_range <= 0:
        return {"body_ratio": 0.0, "upper_wick_pct": 0.0, "lower_wick_pct": 0.0}
    return {
        "body_ratio": number(abs(close - open_price) / candle_range),
        "upper_wick_pct": number((high - max(open_price, close)) / candle_range * 100),
        "lower_wick_pct": number((min(open_price, close) - low) / candle_range * 100),
    }


def volatility_metrics(
    row: pd.Series,
    previous: pd.Series | None,
    atr: float | None,
    close: float | None,
    daily_source: pd.DataFrame,
    requested_at: pd.Timestamp,
    timeframe: str,
    config: dict,
) -> dict:
    true_range = None
    if previous is not None:
        previous_close = finite_float(previous.get("close"))
        high = finite_float(row.get("high"))
        low = finite_float(row.get("low"))
        if None not in (previous_close, high, low):
            true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
    return {
        "atr_14": number(atr),
        "atr_percent": number(atr / close * 100 if atr is not None and close else None),
        "daily_atr_14": number(
            atr if timeframe == "1D" else daily_atr(daily_source, requested_at, config)
        ),
        "current_true_range_atr": number(
            true_range / atr if true_range is not None and atr not in (None, 0) else None
        ),
    }


def daily_atr(daily_source: pd.DataFrame, requested_at: pd.Timestamp, config: dict) -> float | None:
    daily = completed_as_of(daily_source, "1D", requested_at)
    if daily.empty:
        return None
    return finite_float(calculate_indicators(daily, config).iloc[-1].get("ATR"))


def recent_path(frame: pd.DataFrame) -> dict:
    closes = frame["close"]
    return {
        "return_3_bars_pct": number(
            (closes.iloc[-1] / closes.iloc[-4] - 1) * 100 if len(closes) >= 4 else None
        ),
        "return_6_bars_pct": number(
            (closes.iloc[-1] / closes.iloc[-7] - 1) * 100 if len(closes) >= 7 else None
        ),
        "highest_close_6": number(closes.tail(6).max() if len(closes) >= 6 else None),
        "lowest_close_6": number(closes.tail(6).min() if len(closes) >= 6 else None),
        "consecutive_up_closes": consecutive_closes(closes, True),
        "consecutive_down_closes": consecutive_closes(closes, False),
    }


def consecutive_closes(closes: pd.Series, upward: bool) -> int:
    count = 0
    for change in reversed(closes.diff().dropna().tolist()):
        if (change > 0) == upward and change != 0:
            count += 1
        else:
            break
    return count


def market_structure(frame: pd.DataFrame, atr: float | None) -> dict:
    window = frame.tail(100)
    current_close = finite_float(window.iloc[-1].get("close")) if not window.empty else None
    swings = confirmed_swings(window)
    swing_highs = [swing for swing in swings if swing[0] == "high"]
    swing_lows = [swing for swing in swings if swing[0] == "low"]
    recent_high = nearby_swing(swing_highs[-1] if swing_highs else None, current_close, atr)
    recent_low = nearby_swing(swing_lows[-1] if swing_lows else None, current_close, atr)

    events = structure_events(window, swings)
    active = active_breakout(events, window, current_close, atr)
    last_event = last_structure_event(events, len(window))
    return {
        "recent_swing_high": recent_high,
        "recent_swing_low": recent_low,
        "active_breakout": (
            {
                "level": number(active["level"]),
                "direction": active["direction"],
                "closes_beyond": active["closes_beyond"],
            }
            if active is not None else None
        ),
        "last_structure_event": last_event,
    }


def confirmed_swings(frame: pd.DataFrame) -> list[tuple[str, int, float, pd.Timestamp]]:
    swings: list[tuple[str, int, float, pd.Timestamp]] = []
    for position in range(2, len(frame) - 2):
        high = float(frame.iloc[position]["high"])
        low = float(frame.iloc[position]["low"])
        other_highs = pd.concat(
            [frame["high"].iloc[position - 2:position], frame["high"].iloc[position + 1:position + 3]]
        )
        other_lows = pd.concat(
            [frame["low"].iloc[position - 2:position], frame["low"].iloc[position + 1:position + 3]]
        )
        if high > other_highs.max():
            swings.append(("high", position, high, frame.index[position]))
        if low < other_lows.min():
            swings.append(("low", position, low, frame.index[position]))
    return swings


def calculate_trend_state(
    frame: pd.DataFrame,
    atr: float | None,
    structure: dict,
) -> dict:
    window = frame.tail(100)
    return classify_trend_state(window, confirmed_swings(window), atr, structure)


def classify_trend_state(
    frame: pd.DataFrame,
    swings: list[tuple[str, int, float, pd.Timestamp]],
    atr: float | None,
    structure: dict,
) -> dict:
    default = {
        "direction": "SIDE",
        "confidence": 0.5,
        "higher_highs": False,
        "higher_lows": False,
        "trend_change_confirmed": False,
    }
    if atr in (None, 0):
        return default
    highs = [swing for swing in swings if swing[0] == "high"]
    lows = [swing for swing in swings if swing[0] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return default

    tolerance = atr * 0.10
    high_change = compare_swing(highs[-1][2], highs[-2][2], tolerance)
    low_change = compare_swing(lows[-1][2], lows[-2][2], tolerance)
    higher_highs = high_change > 0
    higher_lows = low_change > 0
    falling_highs = high_change < 0
    falling_lows = low_change < 0
    overlapping = swings_overlap(highs[-2:], lows[-2:], atr)

    if higher_highs and higher_lows and not overlapping:
        direction = "UP"
    elif falling_highs and falling_lows and not overlapping:
        direction = "DOWN"
    else:
        return {
            **default,
            "confidence": 0.55,
            "higher_highs": higher_highs,
            "higher_lows": higher_lows,
        }

    sequential = sequence_supports(highs, direction, tolerance) and sequence_supports(
        lows, direction, tolerance
    )
    evidence_agrees = structure_evidence_agrees(structure, direction)
    confidence = 0.70
    if sequential:
        confidence = 0.75
    if sequential and evidence_agrees:
        confidence = 0.85

    latest_move = min(
        abs(highs[-1][2] - highs[-2][2]),
        abs(lows[-1][2] - lows[-2][2]),
    )
    if latest_move < 0.5 * atr:
        confidence -= 0.05
    active = structure.get("active_breakout")
    if active is not None and (
        (direction == "UP" and active["direction"] == "below")
        or (direction == "DOWN" and active["direction"] == "above")
    ):
        confidence -= 0.15

    previous_direction = previous_established_direction(highs, lows, tolerance)
    trend_changed = (
        previous_direction in {"UP", "DOWN"}
        and previous_direction != direction
        and protected_swing_break_confirmed(
            frame, highs, lows, previous_direction, atr
        )
    )
    return {
        "direction": direction,
        "confidence": number(min(0.95, max(0.5, confidence))),
        "higher_highs": higher_highs,
        "higher_lows": higher_lows,
        "trend_change_confirmed": trend_changed,
    }


def compare_swing(latest: float, previous: float, tolerance: float) -> int:
    difference = latest - previous
    if difference >= tolerance:
        return 1
    if difference <= -tolerance:
        return -1
    return 0


def swings_overlap(
    highs: list[tuple[str, int, float, pd.Timestamp]],
    lows: list[tuple[str, int, float, pd.Timestamp]],
    atr: float,
) -> bool:
    previous_low, latest_low = lows[-2][2], lows[-1][2]
    previous_high, latest_high = highs[-2][2], highs[-1][2]
    previous_range = previous_high - previous_low
    latest_range = latest_high - latest_low
    if previous_range <= 0 or latest_range <= 0:
        return True
    overlap = max(0.0, min(previous_high, latest_high) - max(previous_low, latest_low))
    overlap_ratio = overlap / min(previous_range, latest_range)
    structural_move = max(
        abs(latest_high - previous_high),
        abs(latest_low - previous_low),
    )
    return overlap_ratio >= 0.80 and structural_move < 0.5 * atr


def sequence_supports(
    swings: list[tuple[str, int, float, pd.Timestamp]],
    direction: str,
    tolerance: float,
) -> bool:
    if len(swings) < 3:
        return False
    comparisons = [
        compare_swing(swings[index][2], swings[index - 1][2], tolerance)
        for index in range(len(swings) - 2, len(swings))
    ]
    expected = 1 if direction == "UP" else -1
    return all(comparison == expected for comparison in comparisons)


def structure_evidence_agrees(structure: dict, direction: str) -> bool:
    expected = "above" if direction == "UP" else "below"
    active = structure.get("active_breakout")
    if active is not None and active["direction"] == expected:
        return True
    event = structure.get("last_structure_event")
    if event is None:
        return False
    agreeing_failure = (
        "failed_breakout_below" if direction == "UP" else "failed_breakout_above"
    )
    return event["type"] in {
        f"breakout_{expected}",
        f"reclaim_{expected}",
        agreeing_failure,
    }


def previous_established_direction(
    highs: list[tuple[str, int, float, pd.Timestamp]],
    lows: list[tuple[str, int, float, pd.Timestamp]],
    tolerance: float,
) -> str | None:
    if len(highs) < 4 or len(lows) < 4:
        return None
    prior_highs = highs[-4:-1]
    prior_lows = lows[-4:-1]
    high_changes = [
        compare_swing(prior_highs[index][2], prior_highs[index - 1][2], tolerance)
        for index in (1, 2)
    ]
    low_changes = [
        compare_swing(prior_lows[index][2], prior_lows[index - 1][2], tolerance)
        for index in (1, 2)
    ]
    if all(change == 1 for change in high_changes + low_changes):
        return "UP"
    if all(change == -1 for change in high_changes + low_changes):
        return "DOWN"
    return None


def protected_swing_break_confirmed(
    frame: pd.DataFrame,
    highs: list[tuple[str, int, float, pd.Timestamp]],
    lows: list[tuple[str, int, float, pd.Timestamp]],
    previous_direction: str,
    atr: float,
) -> bool:
    protected = lows[-2] if previous_direction == "UP" else highs[-2]
    level = protected[2]
    closes = frame["close"].iloc[protected[1] + 3:]
    consecutive = 0
    for close in closes:
        beyond = close < level if previous_direction == "UP" else close > level
        if not beyond:
            consecutive = 0
            continue
        consecutive += 1
        move = level - close if previous_direction == "UP" else close - level
        if consecutive >= 2 or move >= atr:
            return True
    return False


def nearby_swing(
    swing: tuple[str, int, float, pd.Timestamp] | None,
    current_close: float | None,
    atr: float | None,
) -> dict | None:
    if (
        swing is None
        or current_close is None
        or atr in (None, 0)
        or abs(swing[2] - current_close) > 4 * atr
    ):
        return None
    return {"price": number(swing[2]), "time": iso_utc(swing[3])}


def structure_events(
    frame: pd.DataFrame,
    swings: list[tuple[str, int, float, pd.Timestamp]],
) -> list[dict]:
    closes = frame["close"].tolist()
    events: list[dict] = []
    for kind, position, level, _time in swings:
        for candidate in range(position + 3, len(frame)):
            previous_close = closes[candidate - 1]
            current = closes[candidate]
            if kind == "high" and previous_close <= level < current:
                direction = "above"
                events.append(breakout_event(candidate, direction, level, frame.index[candidate]))
                break
            if kind == "low" and previous_close >= level > current:
                direction = "below"
                events.append(breakout_event(candidate, direction, level, frame.index[candidate]))
                break
        breakout = events[-1] if events else None
        if breakout is None or breakout["level"] != level or breakout["swing_kind"] != kind:
            continue
        beyond = 0
        for candidate in range(breakout["position"], len(frame)):
            close = closes[candidate]
            still_beyond = (direction == "above" and close > level) or (
                direction == "below" and close < level
            )
            if still_beyond:
                beyond += 1
                continue
            if candidate > breakout["position"]:
                events.append(
                    {
                        "type": f"failed_breakout_{direction}",
                        "level": level,
                        "position": candidate,
                        "time": frame.index[candidate],
                        "direction": None,
                        "swing_kind": kind,
                    }
                )
                for reclaim_position in range(candidate + 1, len(frame)):
                    reclaim_close = closes[reclaim_position]
                    reclaimed = (direction == "above" and reclaim_close > level) or (
                        direction == "below" and reclaim_close < level
                    )
                    if reclaimed:
                        events.append(
                            {
                                "type": f"reclaim_{direction}",
                                "level": level,
                                "position": reclaim_position,
                                "time": frame.index[reclaim_position],
                                "direction": direction,
                                "swing_kind": kind,
                            }
                        )
                        break
            break
    return deduplicate_events(events)


def breakout_event(
    position: int,
    direction: str,
    level: float,
    time: pd.Timestamp,
) -> dict:
    return {
        "type": f"breakout_{direction}",
        "level": level,
        "position": position,
        "time": time,
        "direction": direction,
        "swing_kind": "high" if direction == "above" else "low",
    }


def deduplicate_events(events: list[dict]) -> list[dict]:
    unique: dict[tuple, dict] = {}
    for event in events:
        key = (event["type"], event["position"], round(event["level"], 10))
        unique[key] = event
    return sorted(unique.values(), key=lambda event: event["position"])


def active_breakout(
    events: list[dict],
    frame: pd.DataFrame,
    current_close: float | None,
    atr: float | None,
) -> dict | None:
    if current_close is None or atr in (None, 0):
        return None
    closes = frame["close"].tolist()
    breakouts = [
        event for event in events
        if event["type"] in {"breakout_above", "breakout_below", "reclaim_above", "reclaim_below"}
    ]
    for event in reversed(breakouts):
        level = event["level"]
        direction = event["direction"]
        if abs(current_close - level) > 4 * atr:
            continue
        beyond = closes[event["position"]:]
        if direction == "above" and all(close > level for close in beyond):
            return {"level": level, "direction": direction, "closes_beyond": len(beyond)}
        if direction == "below" and all(close < level for close in beyond):
            return {"level": level, "direction": direction, "closes_beyond": len(beyond)}
    return None


def last_structure_event(events: list[dict], frame_length: int) -> dict | None:
    if not events:
        return None
    reversals = [
        event for event in events
        if event["type"].startswith(("failed_breakout_", "reclaim_"))
        and frame_length - 1 - event["position"] <= 3
    ]
    event = reversals[-1] if reversals else events[-1]
    return {
        "type": event["type"],
        "level": number(event["level"]),
        "time": iso_utc(event["time"]),
        "bars_ago": frame_length - 1 - event["position"],
    }


def nearest_level(
    levels: list[float],
    current_close: float | None,
    atr: float | None,
    below: bool,
) -> float | None:
    if current_close is None or atr in (None, 0):
        return None
    eligible = [
        level for level in levels
        if ((level < current_close) if below else (level > current_close))
        and abs(level - current_close) <= 4 * atr
    ]
    if not eligible:
        return None
    return max(eligible) if below else min(eligible)


def actionable_levels(
    frame: pd.DataFrame,
    timeframe: str,
    requested_at: pd.Timestamp,
    atr: float | None,
    structure: dict,
) -> dict:
    if frame.empty or atr in (None, 0):
        return {"supports": [], "resistances": []}
    current_close = float(frame.iloc[-1]["close"])
    window = frame.tail(100)
    candidates: list[dict] = []

    for _kind, _position, price, _time in confirmed_swings(window):
        candidates.append({"price": price, "source": "swing"})
    active = structure.get("active_breakout")
    if active is not None:
        candidates.append({"price": active["level"], "source": "breakout"})
    last_event = structure.get("last_structure_event")
    if last_event is not None:
        candidates.append({"price": last_event["level"], "source": "breakout"})
    candidates.extend(period_levels(frame, timeframe, requested_at))

    priority = {"breakout": 0, "swing": 1, "weekly": 2, "pivot": 3}
    deduplicated: list[dict] = []
    tolerance = atr * 0.05
    for candidate in sorted(candidates, key=lambda item: priority[item["source"]]):
        price = finite_float(candidate["price"])
        if price is None or abs(price - current_close) > 4 * atr:
            continue
        if any(abs(price - existing["price"]) <= tolerance for existing in deduplicated):
            continue
        deduplicated.append({"price": price, "source": candidate["source"]})

    supports = [candidate for candidate in deduplicated if candidate["price"] <= current_close]
    resistances = [candidate for candidate in deduplicated if candidate["price"] > current_close]
    supports.sort(key=lambda item: current_close - item["price"])
    resistances.sort(key=lambda item: item["price"] - current_close)
    return {
        "supports": [format_level(item, current_close, atr, window) for item in supports[:2]],
        "resistances": [
            format_level(item, current_close, atr, window) for item in resistances[:2]
        ],
    }


def period_levels(
    frame: pd.DataFrame,
    timeframe: str,
    requested_at: pd.Timestamp,
) -> list[dict]:
    aggregation = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    weeks = frame.resample("W-MON", label="left", closed="left").agg(aggregation)
    completed_weeks = weeks.loc[(weeks.index + pd.Timedelta(days=7)) <= requested_at].dropna()
    levels: list[dict] = []
    if not completed_weeks.empty:
        week = completed_weeks.iloc[-1]
        levels.extend(
            [
                {"price": float(week["high"]), "source": "weekly"},
                {"price": float(week["low"]), "source": "weekly"},
            ]
        )

    if timeframe in {"1h", "4h"}:
        pivot_source = completed_weeks.iloc[-1] if not completed_weeks.empty else None
    else:
        months = frame.resample("MS", label="left", closed="left").agg(aggregation)
        completed_months = months.loc[
            (months.index + pd.offsets.MonthBegin(1)) <= requested_at
        ].dropna()
        pivot_source = completed_months.iloc[-1] if not completed_months.empty else None
    if pivot_source is not None:
        high = float(pivot_source["high"])
        low = float(pivot_source["low"])
        close = float(pivot_source["close"])
        pivot = (high + low + close) / 3
        levels.extend(
            [
                {"price": pivot, "source": "pivot"},
                {"price": 2 * pivot - low, "source": "pivot"},
                {"price": 2 * pivot - high, "source": "pivot"},
            ]
        )
    return levels


def format_level(candidate: dict, current_close: float, atr: float, frame: pd.DataFrame) -> dict:
    price = candidate["price"]
    touch_tolerance = atr * 0.15
    touches = int(
        ((frame["low"] <= price + touch_tolerance) & (frame["high"] >= price - touch_tolerance)).sum()
    )
    return {
        "price": number(price),
        "distance_atr": number(abs(current_close - price) / atr),
        "source": candidate["source"],
        "touches": touches,
    }


def bar_signal(
    row: pd.Series,
    previous: pd.Series | None,
    structure: dict,
    levels: dict,
    atr: float | None,
) -> dict:
    empty = {
        "type": "none",
        "direction": "neutral",
        "level": None,
        "confirmation_count": 0,
        "confidence": 0.0,
    }
    close = finite_float(row.get("close"))
    if close is None or atr in (None, 0):
        return empty

    event = structure.get("last_structure_event")
    if event is not None and event["bars_ago"] == 0:
        event_type = event["type"]
        if event_type.startswith("failed_breakout_"):
            direction = "down" if event_type.endswith("above") else "up"
            return signal_result("failed_breakout", direction, event["level"], 1, row)
        if event_type.startswith("reclaim_"):
            direction = "up" if event_type.endswith("above") else "down"
            return signal_result("reclaim", direction, event["level"], 1, row)

    active = structure.get("active_breakout")
    if active is not None and active["closes_beyond"] <= 3:
        direction = "up" if active["direction"] == "above" else "down"
        return signal_result(
            "breakout", direction, active["level"], active["closes_beyond"], row
        )

    candle = candle_metrics(row)
    nearest = closest_level(levels, close)
    near_level = (
        nearest is not None and abs(close - nearest["price"]) / atr <= 0.5
    )
    if previous is not None:
        bullish_reversal = (
            previous["close"] < previous["open"]
            and row["close"] > row["open"]
            and row["close"] >= previous["open"]
            and row["open"] <= previous["close"]
        )
        bearish_reversal = (
            previous["close"] > previous["open"]
            and row["close"] < row["open"]
            and row["close"] <= previous["open"]
            and row["open"] >= previous["close"]
        )
        if bullish_reversal:
            return signal_result(
                "bullish_reversal", "up", nearest["price"] if near_level else None, 2, row
            )
        if bearish_reversal:
            return signal_result(
                "bearish_reversal", "down", nearest["price"] if near_level else None, 2, row
            )
    if near_level and candle["lower_wick_pct"] is not None and candle["lower_wick_pct"] >= 50:
        return signal_result("bullish_rejection", "up", nearest["price"], 1, row)
    if near_level and candle["upper_wick_pct"] is not None and candle["upper_wick_pct"] >= 50:
        return signal_result("bearish_rejection", "down", nearest["price"], 1, row)
    return empty


def signal_result(
    signal_type: str,
    direction: str,
    level,
    confirmation_count: int,
    row: pd.Series,
) -> dict:
    confidence = 0.55
    volume_ratio = safe_ratio(row.get("volume"), row.get("volume.SMA20"))
    adx = finite_float(row.get("ADX"))
    if volume_ratio is not None and volume_ratio >= 1.5:
        confidence += 0.15
    if adx is not None and adx >= 25:
        confidence += 0.15
    if level is not None:
        confidence += 0.05
    if confirmation_count >= 2:
        confidence += 0.05
    return {
        "type": signal_type,
        "direction": direction,
        "level": number(level),
        "confirmation_count": confirmation_count,
        "confidence": number(min(confidence, 0.95)),
    }


def level_interaction(
    frame: pd.DataFrame,
    structure: dict,
    levels: dict,
    atr: float | None,
) -> dict:
    empty = {
        "level": None,
        "event": None,
        "distance_atr": None,
        "consecutive_closes_beyond": 0,
    }
    if frame.empty or atr in (None, 0):
        return empty
    close = float(frame.iloc[-1]["close"])
    previous_close = float(frame.iloc[-2]["close"]) if len(frame) >= 2 else None
    event = structure.get("last_structure_event")
    if event is not None and event["bars_ago"] == 0:
        interaction = "reclaim" if event["type"].startswith("reclaim_") else "reject"
        return interaction_result(event["level"], interaction, close, atr, 0)

    all_levels = [
        {**level, "side": side}
        for side in ("supports", "resistances")
        for level in levels[side]
    ]
    if not all_levels:
        return empty
    if previous_close is not None:
        crossings = []
        for level in all_levels:
            price = level["price"]
            crossed = (
                (previous_close <= price < close)
                or (previous_close >= price > close)
            )
            if crossed:
                crossings.append(level)
        if crossings:
            selected = min(crossings, key=lambda item: abs(close - item["price"]))
            count = consecutive_beyond(frame["close"], selected["price"], close > selected["price"])
            return interaction_result(selected["price"], "close_beyond", close, atr, count)

    selected = min(all_levels, key=lambda item: abs(close - item["price"]))
    distance = abs(close - selected["price"]) / atr
    interaction = "touch" if distance <= 0.15 else "approach"
    return interaction_result(selected["price"], interaction, close, atr, 0)


def interaction_result(level, event: str, close: float, atr: float, count: int) -> dict:
    return {
        "level": number(level),
        "event": event,
        "distance_atr": number(abs(close - float(level)) / atr),
        "consecutive_closes_beyond": count,
    }


def consecutive_beyond(closes: pd.Series, level: float, above: bool) -> int:
    count = 0
    for close in reversed(closes.tolist()):
        if (close > level) if above else (close < level):
            count += 1
        else:
            break
    return count


def closest_level(levels: dict, close: float) -> dict | None:
    flattened = levels["supports"] + levels["resistances"]
    return min(flattened, key=lambda item: abs(close - item["price"])) if flattened else None


def session_context(frame: pd.DataFrame, timeframe: str, asset: str) -> dict | None:
    symbol, _exchange = split_asset(asset)
    if not symbol.endswith("!"):
        return None
    duration = TIMEFRAME_DURATIONS[timeframe]
    closes = pd.DatetimeIndex(frame.index + duration)
    if len(closes) < 2:
        return {"next_expected_bar_close": None, "gap_expected": None}
    latest = closes[-1]
    matching_deltas = [
        closes[position + 1] - closes[position]
        for position in range(len(closes) - 1)
        if closes[position].weekday() == latest.weekday()
        and closes[position].hour == latest.hour
    ]
    if not matching_deltas:
        return {"next_expected_bar_close": None, "gap_expected": None}
    counts = pd.Series(matching_deltas).value_counts()
    expected_delta = min(counts[counts == counts.max()].index)
    return {
        "next_expected_bar_close": iso_utc(latest + expected_delta),
        "gap_expected": bool(expected_delta > duration),
    }


def week_to_date(
    frame: pd.DataFrame,
    requested_at: pd.Timestamp,
    source_timeframe: str,
) -> dict:
    week_start = requested_at.normalize() - pd.Timedelta(days=requested_at.weekday())
    week = frame.loc[frame.index >= week_start]
    empty = {
        "source_timeframe": source_timeframe,
        "week_open": None,
        "return_pct": None,
        "highest_close": None,
        "lowest_close": None,
        "drawdown_from_highest_close_pct": None,
        "rebound_from_lowest_close_pct": None,
    }
    if week.empty:
        return empty
    week_open = finite_float(week.iloc[0].get("open"))
    close = finite_float(week.iloc[-1].get("close"))
    highest = finite_float(week["close"].max())
    lowest = finite_float(week["close"].min())
    return {
        "source_timeframe": source_timeframe,
        "week_open": number(week_open),
        "return_pct": number(percent_change(week_open, close)),
        "highest_close": number(highest),
        "lowest_close": number(lowest),
        "drawdown_from_highest_close_pct": number(
            (highest / close - 1) * 100 if highest is not None and close else None
        ),
        "rebound_from_lowest_close_pct": number(
            (close / lowest - 1) * 100 if close is not None and lowest else None
        ),
    }


def weekly_vwap(
    hourly: pd.DataFrame,
    requested_at: pd.Timestamp,
    atr: float | None,
    current_close: float | None,
) -> dict:
    week_start = requested_at.normalize() - pd.Timedelta(days=requested_at.weekday())
    week = hourly.loc[hourly.index >= week_start]
    volumes = week["volume"] if not week.empty else pd.Series(dtype=float)
    if not volume_is_reliable(volumes) or current_close is None:
        return {"value": None, "distance_pct": None, "distance_atr": None}
    typical = (week["high"] + week["low"] + week["close"]) / 3
    value = float((typical * volumes).sum() / volumes.sum())
    return {
        "value": number(value),
        "distance_pct": number((current_close - value) / value * 100 if value else None),
        "distance_atr": number(
            (current_close - value) / atr if atr not in (None, 0) else None
        ),
    }


def momentum_change(row: pd.Series, previous: pd.Series | None, atr: float | None) -> dict:
    histogram = difference(row.get("MACD.macd"), row.get("MACD.signal"))
    previous_histogram = (
        difference(previous.get("MACD.macd"), previous.get("MACD.signal"))
        if previous is not None else None
    )
    ema_change = (
        difference(row.get("EMA20"), previous.get("EMA20")) if previous is not None else None
    )
    return {
        "macd_histogram": number(histogram),
        "previous_macd_histogram": number(previous_histogram),
        "adx_previous": number(previous.get("ADX")) if previous is not None else None,
        "ema20_change_atr": number(
            ema_change / atr if ema_change is not None and atr not in (None, 0) else None
        ),
    }


def data_quality(frame: pd.DataFrame, timeframe: str, asset: str) -> dict:
    _symbol, exchange = split_asset(asset)
    gaps = frame.index.to_series().diff().dropna()
    gap_rows = gaps[gaps > TIMEFRAME_DURATIONS[timeframe]]
    continuous_market = exchange in {"BITSTAMP", "BINANCE"}
    scheduled = (
        0 if continuous_market else
        sum(gap_overlaps_weekend(end - gap, end) for end, gap in gap_rows.items())
    )
    if gap_rows.empty:
        unexpected = False
    elif continuous_market:
        unexpected = True
    else:
        unexpected = None
    return {
        "bars_available": len(frame),
        "unexpected_missing_bars": unexpected,
        "scheduled_session_gaps": scheduled,
        "volume_reliable": volume_is_reliable(frame["volume"]),
    }


def gap_overlaps_weekend(start: pd.Timestamp, end: pd.Timestamp) -> int:
    cursor = start + pd.Timedelta(hours=1)
    while cursor < end:
        if cursor.weekday() >= 5:
            return 1
        cursor += pd.Timedelta(hours=1)
    return 0


def volume_is_reliable(volume: pd.Series) -> bool:
    return bool(
        not volume.empty
        and volume.notna().all()
        and (volume >= 0).all()
        and float(volume.sum()) > 0
    )


def finite_float(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def number(value):
    converted = finite_float(value)
    return round(converted, 2) if converted is not None else None


def safe_ratio(numerator, denominator) -> float | None:
    numerator_value = finite_float(numerator)
    denominator_value = finite_float(denominator)
    if numerator_value is None or denominator_value in (None, 0):
        return None
    return numerator_value / denominator_value


def percent_change(start, end) -> float | None:
    start_value = finite_float(start)
    end_value = finite_float(end)
    if start_value in (None, 0) or end_value is None:
        return None
    return (end_value - start_value) / start_value * 100


def difference(left, right) -> float | None:
    left_value = finite_float(left)
    right_value = finite_float(right)
    if left_value is None or right_value is None:
        return None
    return left_value - right_value
