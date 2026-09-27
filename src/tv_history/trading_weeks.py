"""Recurring trading-week windows learned only from hourly timestamps."""
from collections import Counter

import pandas as pd

from .market_rules import resolve_market_rule

VERSION = 2


def _monday(local, weekend_belongs_to_next_week):
    day = local.tz_localize(None).normalize()
    monday = day - pd.Timedelta(days=day.weekday())
    if weekend_belongs_to_next_week and day.weekday() >= 5:
        monday += pd.Timedelta(days=7)
    return monday


def build_trading_weeks(asset, frames, settings, now):
    """Learn the most frequent repeated weekly opening/closing pair.

    Futures weekend openings belong to the following Monday-labelled week.
    This is a grouping convention, not a holiday ownership inference.
    Edge weeks are excluded so partial/current weeks cannot train the rule.
    """
    rule = resolve_market_rule(settings, asset)
    weekend = rule.timestamp_profile != "utc_calendar"
    result = {"schema_version": VERSION, "timezone": rule.timezone,
              "weekend_belongs_to_next_week": weekend, "built_at_utc": now.isoformat(),
              "status": "unavailable"}
    if not rule.timezone:
        result["reason"] = "no_timezone"
        return result
    hourly = frames["1h"]
    index = hourly.index[(hourly.index < now) & (hourly.index >= now - pd.Timedelta(days=settings.calendar_lookback_days))]
    if len(index) < 2:
        result["reason"] = "insufficient_hourly_history"
        return result
    groups = {}
    for stamp in index.tz_convert(rule.timezone):
        groups.setdefault(_monday(stamp, weekend), []).append(stamp)
    samples = []
    for monday in sorted(groups)[1:-1]:
        stamps = groups[monday]
        opened = stamps[0].tz_localize(None)
        closed = (stamps[-1] + pd.Timedelta(hours=1)).tz_localize(None)
        samples.append((int((opened - monday).total_seconds() // 60),
                        int((closed - monday).total_seconds() // 60)))
    counts = Counter(samples)
    result["weeks_examined"] = len(samples)
    if not counts:
        result["reason"] = "insufficient_complete_weeks"
        return result
    pattern, count = counts.most_common(1)[0]
    result["matching_weeks"] = count
    tied = sum(n == count for n in counts.values()) > 1
    if count < settings.calendar_min_observations or tied:
        result["reason"] = "no_recurring_week_boundary"
        return result
    def boundary(minutes):
        day, minute = divmod(minutes, 1440)
        return {"day_offset": day, "time": f"{minute // 60:02d}:{minute % 60:02d}"}
    result.update(status="ready", open=boundary(pattern[0]), close=boundary(pattern[1]),
                  training_from_utc=index[0].isoformat(), training_through_utc=index[-1].isoformat())
    return result


def hourly_trading_week(calendar, timestamp):
    """Assign a supplied hourly opening, including future bars, to its normal week.

    This does not assert a bar exists or that a holiday session is open.
    Outside the learned window, return None.
    """
    stamp = pd.Timestamp(timestamp)
    if stamp.tzinfo is None:
        raise ValueError("Hourly timestamp must be timezone-aware")
    rule = calendar.get("trading_weeks", {})
    if rule.get("schema_version") != VERSION or rule.get("status") != "ready":
        return None
    local = stamp.tz_convert(rule["timezone"])
    monday = _monday(local, rule["weekend_belongs_to_next_week"])
    bounds = trading_week_bounds(calendar, monday)
    if bounds and pd.Timestamp(bounds["start_utc"]) <= stamp < pd.Timestamp(bounds["end_utc"]):
        return bounds
    return None


def trading_week_bounds(calendar, monday):
    """Resolve a Monday week label without treating midnight as a session open."""
    rule = calendar.get("trading_weeks", {})
    if rule.get("schema_version") != VERSION or rule.get("status") != "ready":
        return None
    monday = pd.Timestamp(monday)
    def boundary(part):
        hour, minute = map(int, part["time"].split(":"))
        return (monday + pd.Timedelta(days=part["day_offset"], hours=hour, minutes=minute)).tz_localize(
            rule["timezone"], ambiguous="NaT", nonexistent="NaT")
    start, end = boundary(rule["open"]), boundary(rule["close"])
    if pd.isna(start) or pd.isna(end):
        return None
    return {"week": monday.date().isoformat(), "status": "recurring_rule",
            "start_utc": start.tz_convert("UTC").isoformat(), "end_utc": end.tz_convert("UTC").isoformat(),
            "start_local": start.isoformat(), "end_local": end.isoformat()}
