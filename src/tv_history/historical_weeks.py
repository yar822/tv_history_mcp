"""Shared observed hourly week boundaries for sessions and completion."""
import pandas as pd

from .trading_weeks import _monday

HOUR = pd.Timedelta(hours=1)


def week_phase(monday, zone, weekend, now):
    current = _monday(now.tz_convert(zone), weekend)
    return "ended" if monday < current else "future" if monday > current else "ongoing"


def observed_history(index, coverage, monday, zone, weekend, now, expected):
    """Accept bracketed past weeks unless coverage proves missing history.

    Unknown gaps remain uncertainty, not evidence of missing bars. Observed
    boundaries reconstruct stored history, not an as-received historical feed.
    """
    evidence = {"coverage": "uncertain", "cached_bars_in_week": 0}
    try:
        phase = week_phase(monday, zone, weekend, now)
        if phase != "ended":
            return None, phase + "_week", evidence
        index = index[index < now].sort_values().unique()
        if index.empty:
            return None, "history_unavailable", evidence
        iso = lambda t: t.tz_convert("UTC").isoformat().replace("+00:00", "Z")
        evidence.update(cache_first_utc=iso(index[0]), cache_last_utc=iso(index[-1]))
        # Vectorized ownership labels avoid a Python loop over the full cache.
        days = index.tz_convert(zone).tz_localize(None).normalize()
        labels = days - pd.to_timedelta(days.weekday, unit="D")
        if weekend:
            labels += pd.to_timedelta((days.weekday >= 5).astype(int) * 7, unit="D")
        selected = index[labels == monday]
        evidence["cached_bars_in_week"] = len(selected)
        if selected.empty:
            return None, "history_unavailable", evidence
        if not ((labels < monday).any() and (labels > monday).any()):
            return None, "incomplete_history", evidence
        if "unresolved_gaps" not in coverage:
            return None, "coverage_unknown", evidence
        start, end = selected[0], selected[-1] + HOUR
        if end > now:
            return None, "incomplete_history", evidence
        check_start = min(start, pd.Timestamp(expected["start_utc"])) if expected else start
        check_end = max(end, pd.Timestamp(expected["end_utc"])) if expected else end
        gaps = [g for g in coverage["unresolved_gaps"]
                if pd.Timestamp(g["after"]) < check_end and pd.Timestamp(g.get("before", now)) > check_start]
        confirmed = sum(g.get("status") == "incomplete" for g in gaps)
        evidence.update(unresolved_gap_count=len(gaps), confirmed_missing_gap_count=confirmed,
                        uncertain_1h_gap_count=len(gaps) - confirmed)
        if confirmed:
            evidence["coverage"] = "incomplete"
            return None, "incomplete_history", evidence
        evidence["coverage"] = "uncertain" if gaps else "no_known_gaps"
        return selected, None, evidence
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, "history_unreadable", evidence
