"""Dated weekly/daily windows with conservative observed-to-expected fallback."""
import pandas as pd

from .metadata import DAYS
from .trading_weeks import VERSION, _monday, trading_week_bounds
from .historical_weeks import observed_history, week_phase


HOUR = pd.Timedelta(hours=1)


def _iso(stamp):
    return stamp.tz_convert("UTC").isoformat().replace("+00:00", "Z")


def _boundary(stamp, utc=False):
    return {"day": DAYS[stamp.weekday()], "time": stamp.strftime("%H:%M:%S") + ("Z" if utc else ""),
            "datetime": _iso(stamp) if utc else stamp.isoformat()}


def _window(start, end, zone, basis):
    if end <= start:
        raise ValueError("Session end must follow its opening")
    return {"open": _boundary(start.tz_convert("UTC"), True),
            "close": _boundary(end.tz_convert("UTC"), True),
            "local": {"open": _boundary(start.tz_convert(zone)), "close": _boundary(end.tz_convert(zone))},
            "basis": basis}


def _local(day, clock, zone):
    hour, minute = map(int, clock.split(":"))
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError("Invalid session clock")
    return (day + pd.Timedelta(hours=hour, minutes=minute)).tz_localize(
        zone, ambiguous="NaT", nonexistent="NaT")


def _history(storage, asset, monday, zone, weekend, now, expected):
    try:
        frame = storage.read(asset, "1h")
        coverage = storage.read_coverage(asset).get("1h", {})
        return observed_history(frame.index, coverage, monday, zone, weekend, now, expected)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, "history_unreadable", {"coverage": "uncertain", "cached_bars_in_week": 0}


def dated_sessions(storage, asset, session, date, calendar, now):
    monday = pd.Timestamp(date)
    monday -= pd.Timedelta(days=monday.weekday())
    now = pd.Timestamp(now).tz_convert("UTC")
    rule = calendar.get("trading_weeks", {})
    zone = rule.get("timezone") or calendar.get("timezone")
    result = {"asset": asset, "session": session, "requested_date": date,
              "week": monday.date().isoformat(), "timezone": "UTC", "local_timezone": zone,
              "status": "unavailable", "sessions": [], "metadata": {
                  "utc_basis": "boundary_dates", "evaluated_at_utc": _iso(now),
                  "close_basis": "last_hourly_open_plus_1h"},
              "notes": ["No holiday calendar is available; expected boundaries do not account for holidays or exceptional closures.",
                        "Open is inclusive; close is exclusive, inferred at hourly resolution."]}
    metadata = result["metadata"]

    def unavailable(reason):
        result["reason"] = reason
        return result

    if not zone:
        return unavailable("no_timezone")
    if rule.get("schema_version") != VERSION or type(rule.get("weekend_belongs_to_next_week")) is not bool:
        return unavailable("week_ownership_unavailable")
    weekend = rule["weekend_belongs_to_next_week"]
    expected = trading_week_bounds(calendar, monday)
    if rule.get("status") == "ready" and expected is None:
        return unavailable("ambiguous_or_nonexistent_boundary")
    phase = week_phase(monday, zone, weekend, now)
    metadata["week_state"] = phase
    selected = None
    if phase == "ended":
        selected, reason, history = _history(storage, asset, monday, zone, weekend, now, expected)
        metadata.update(history)
    else:
        reason = phase + "_week"
        metadata["coverage"] = "not_evaluated"

    if selected is not None:
        if session == "week":
            spans = [(selected[0], selected[-1] + HOUR)]
        else:
            if calendar.get("status") == "no_confirmed_breaks":
                return unavailable("no_learned_daily_sessions")
            # Match the existing learner's physical-gap definition.
            cuts = [0] + [i for i in range(1, len(selected)) if selected[i] - selected[i - 1] > HOUR] + [len(selected)]
            spans = [(selected[a], selected[b - 1] + HOUR) for a, b in zip(cuts, cuts[1:])]
        result.update(status="observed", sessions=[_window(a, b, zone, "observed") for a, b in spans])
        metadata.update(source="cached_1h_timestamps", method="observed_weekly_bounds" if session == "week" else "observed_contiguous_hourly_runs",
                        history_fallback=False)
        result["notes"].append("Observed boundaries describe cached bars; no_known_gaps does not guarantee complete exchange history.")
        if metadata["coverage"] == "uncertain":
            result["notes"].append("Unexplained gaps may be holidays, ordinary closures or missing data; their cause is unknown. Observed daily windows are cached hourly runs, not confirmed exchange sessions.")
        return result

    metadata.update(source="saved_calendar", history_fallback=phase == "ended", expectation_reason=reason)
    if phase == "ended":
        result["notes"].append("Historical evidence is incomplete or unavailable; these boundaries are expectations, not observed history.")
    spans = []
    if session == "week":
        if not expected:
            return unavailable("week_rule_unavailable")
        spans = [(pd.Timestamp(expected["start_utc"]), pd.Timestamp(expected["end_utc"]))]
        metadata["method"] = "most_frequent_weekly_boundary_pair"
        for key in ("matching_weeks", "weeks_examined", "built_at_utc", "training_through_utc"):
            if key in rule:
                metadata[key] = _iso(pd.Timestamp(rule[key])) if key.endswith("_utc") else rule[key]
    else:
        if calendar.get("schema_version") != 2 or calendar.get("status") != "ready" or not calendar.get("sessions"):
            return unavailable("no_learned_daily_sessions")
        for day, entries in calendar["sessions"].items():
            day_number = DAYS.index(day)
            day_offset = day_number - 7 if weekend and day_number >= 5 else day_number
            base = monday + pd.Timedelta(days=day_offset)
            for entry in entries:
                opened = _local(base + pd.Timedelta(days=entry.get("start_day_offset", 0)), entry["start"], zone)
                closed = _local(base + pd.Timedelta(days=entry.get("end_day_offset", 0)), entry["end"], zone)
                if pd.isna(opened) or pd.isna(closed):
                    return unavailable("ambiguous_or_nonexistent_boundary")
                if _monday(opened, weekend) == monday:
                    spans.append((opened, closed))
        if not spans:
            return unavailable("no_learned_daily_sessions")
        metadata.update(method="recurring_contiguous_hourly_runs", template_coverage="observed_templates_only")
        for target, source in (("built_at_utc", "built_at"), ("training_through_utc", "training_through"), ("eligible_sessions", "eligible_sessions")):
            if source in calendar:
                metadata[target] = _iso(pd.Timestamp(calendar[source])) if target.endswith("_utc") else calendar[source]
        result["notes"].append("Daily template coverage is partial; missing weekdays are unknown, not confirmed closures.")
    result.update(status="expected", sessions=[_window(a, b, zone, "expected") for a, b in sorted(spans)])
    return result
