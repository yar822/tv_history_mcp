"""Frozen, evidence-based native candle boundaries. No market API or timers.

Rebuilt at process launch when cached inputs changed, and at ticker initialization.
Labels are never used as session opens. All inference uses raw source timestamps.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json

import pandas as pd

from .market_rules import resolve_market_rule


HOUR = pd.Timedelta(hours=1)
DURATIONS = {"1h": HOUR, "4h": 4 * HOUR, "1D": 24 * HOUR, "1W": 168 * HOUR}


def slot(stamp, zone):
    local = stamp.tz_convert(zone)
    return f"{local.weekday()}:{local:%H:%M}"


def shape(index, zone):
    local = index.tz_convert(zone)
    first = local[0].date()
    return tuple(((t.date() - first).days, t.hour, t.minute) for t in local)


def offset(opened, closed, zone):
    a, b = opened.tz_convert(zone), closed.tz_convert(zone)
    return ((b.date() - a.date()).days, b.hour, b.minute)


def boundary_from_rule(opened, rule, zone):
    # Build a local civil time, rather than adding 24-hour days across DST.
    local = opened.tz_convert(zone).tz_localize(None).normalize()
    day, hour, minute = rule
    return (local + pd.Timedelta(days=day, hours=hour, minutes=minute)).tz_localize(
        zone, ambiguous="raise", nonexistent="raise"
    ).tz_convert("UTC")


def learn_calendar(asset, frames, settings, now):
    zone = resolve_market_rule(settings, asset).timezone
    result = {"schema_version": 1, "asset": asset, "timezone": zone,
              "built_at": now.isoformat(), "effective_from": now.isoformat(),
              "requested_sessions": settings.calendar_sessions, "eligible_sessions": 0,
              "rules": {}, "session_templates": {}, "status": "insufficient_history",
              "boundary_overrides": (settings.calendar_boundary_overrides or {}).get(asset, {})}
    hourly = frames["1h"]
    if not zone or hourly.empty:
        return finish(result)
    hourly = hourly.loc[(hourly.index < now) &
                        (hourly.index >= now - pd.Timedelta(days=settings.calendar_lookback_days))]
    if len(hourly) < 3:
        return finish(result)
    # A session is a contiguous hourly run separated by a physical gap.
    # A continuous market has no physical boundary exception.
    groups = hourly.index.to_series().diff().gt(HOUR).cumsum()
    segments = [part for _, part in hourly.groupby(groups)]
    if len(segments) < 3:
        result["status"] = "no_confirmed_breaks"
        return finish(result)
    # Both cache-edge runs are excluded; the last has no observed session successor.
    candidates = [s for s in segments[1:-1] if len(s) <= 30]
    recent_dates = sorted({p.index[0].tz_convert(zone).date() for p in candidates})[-settings.calendar_sessions:]
    counts = defaultdict(Counter)
    for part in candidates:
        start = part.index[0].tz_convert(zone)
        if start.date() not in recent_dates:
            continue
        counts[(start.weekday(), start.hour, start.minute)][shape(part.index, zone)] += 1
    templates = defaultdict(list)
    for (weekday, _hour, _minute), counter in counts.items():
        pattern, count = counter.most_common(1)[0]
        if count >= settings.calendar_min_observations and count / sum(counter.values()) >= settings.calendar_min_agreement:
            templates[weekday].append(pattern)
    # Count trading-session dates, not separate runs around a lunch break.
    by_date = defaultdict(list)
    for part in candidates:
        by_date[part.index[0].tz_convert(zone).date()].append(part)
    good_dates = [date for date, parts in by_date.items() if
                  templates.get(date.weekday()) and
                  Counter(shape(p.index, zone) for p in parts) == Counter(templates[date.weekday()])]
    good_dates = good_dates[-settings.calendar_sessions:]
    good = [p for date in good_dates for p in by_date[date]]
    result["eligible_sessions"] = len(good_dates)
    result["rejected_sessions"] = len(candidates) - sum(
        shape(p.index, zone) in templates.get(p.index[0].tz_convert(zone).weekday(), []) for p in candidates)
    result["session_templates"] = {str(k): v for k, v in templates.items()}
    if len(good_dates) < settings.calendar_sessions:
        return finish(result)
    result["training_from"] = good[0].index[0].isoformat()
    result["training_through"] = (good[-1].index[-1] + HOUR).isoformat()
    starts = {p.index[0] for p in good}
    ends = {p.index[-1] + HOUR for p in good}
    allowed = set(t for p in good for t in p.index)
    first, last = good[0].index[0], good[-1].index[-1] + HOUR
    observations = defaultdict(list)
    for tf, frame in frames.items():
        for i in range(len(frame) - 1):
            opened, successor = frame.index[i:i + 2]
            if opened < first - HOUR or successor > now or opened >= last:
                continue
            end = min(opened + DURATIONS[tf], successor) if tf in {"1h", "4h"} else successor
            part = hourly.loc[(hourly.index >= opened) & (hourly.index < end)]
            key = f"{tf}|{slot(opened, zone)}"
            learned = None
            if not part.empty and all(t in allowed for t in part.index):
                close = part.index[-1] + HOUR
                # Only physical session ends, with native OHLC ownership evidence.
                row = frame.iloc[i]
                match = all(abs(float(a) - float(b)) <= 1e-8 for a, b in (
                    (row.open, part.iloc[0].open), (row.high, part.high.max()),
                    (row.low, part.low.min())))
                complete_coverage = True
                # Reject missing entire sessions inside a daily/weekly interval.
                for date in pd.date_range(opened.tz_convert(zone).date(), end.tz_convert(zone).date()):
                    for template in templates.get(date.weekday(), []):
                        d, h, m = template[0]
                        try:
                            expected = (date + pd.Timedelta(days=d, hours=h, minutes=m)).tz_localize(zone).tz_convert("UTC")
                        except ValueError:
                            complete_coverage = False
                            continue
                        if opened <= expected < close and expected not in starts:
                            complete_coverage = False
                if close in ends and match and complete_coverage and close <= successor:
                    next_position = hourly.index.searchsorted(close)
                    if next_position < len(hourly):
                        learned = (offset(opened, close, zone),
                                   offset(opened, hourly.index[next_position], zone))
            observations[key].append(learned)
    for key, samples in observations.items():
        counter = Counter(samples)
        pattern, count = counter.most_common(1)[0]
        # Recent contradictory evidence disables a previously dominant boundary.
        if pattern is not None and samples[-1] == pattern and count >= settings.calendar_min_observations and count / len(samples) >= settings.calendar_min_agreement:
            result["rules"][key] = {"offset": pattern[0], "next_offset": pattern[1], "observations": count,
                                    "total": len(samples), "confidence": count / len(samples)}
    result["status"] = "ready"
    return finish(result)


def finish(result):
    result["version"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:16]
    return result


def expected_boundary(calendar, timeframe, opened, evaluated_at):
    if not calendar or evaluated_at < pd.Timestamp(calendar["effective_from"]):
        return None
    override = calendar.get("boundary_overrides", {}).get(timeframe, {}).get(opened.isoformat())
    if override:
        boundary = pd.Timestamp(override)
        if boundary.tzinfo is None or boundary <= opened:
            raise ValueError("Boundary override must be an aware close after its raw open")
        return boundary.tz_convert("UTC")
    if calendar.get("status") != "ready":
        return None
    zone = calendar["timezone"]
    rule = calendar["rules"].get(f"{timeframe}|{slot(opened, zone)}")
    if f"{timeframe}|{slot(opened, zone)}" in calendar.get("suspended_rules", {}):
        return None
    if not rule:
        return None
    try:
        return boundary_from_rule(opened, rule["offset"], zone)
    except ValueError:
        return None


def suspend_conflicts(calendar, frames, observed_hourly):
    """Suspend contradicted rules without learning/rebuilding a runtime calendar."""
    if not calendar or calendar.get("status") != "ready" or observed_hourly.empty:
        return False
    changed = False
    zone = calendar["timezone"]
    cutoff = pd.Timestamp(calendar["effective_from"])
    for tf, frame in frames.items():
        for opened in frame.index[-20:]:
            key = f"{tf}|{slot(opened, zone)}"
            rule = calendar["rules"].get(key)
            if not rule or key in calendar.get("suspended_rules", {}):
                continue
            try:
                close = boundary_from_rule(opened, rule["offset"], zone)
                resume = boundary_from_rule(opened, rule["next_offset"], zone)
            except ValueError:
                continue
            contradiction = observed_hourly.index[(observed_hourly.index >= max(close, cutoff)) &
                                                   (observed_hourly.index < resume)]
            if len(contradiction):
                calendar.setdefault("suspended_rules", {})[key] = {
                    "reason": "bar_received_during_predicted_break",
                    "bar_open": contradiction[0].isoformat()}
                changed = True
    return changed
