"""Conservative coverage checks on native timestamps; never invent missing bars."""
import pandas as pd

from .calendar import DURATIONS, boundary_from_rule, slot


def scheduled_gap(calendar, timeframe, before, after):
    if not calendar or calendar.get("status") != "ready":
        return False
    key = f"{timeframe}|{slot(before, calendar['timezone'])}"
    rule = calendar.get("rules", {}).get(key)
    if not rule or key in calendar.get("suspended_rules", {}):
        return False
    try:
        # Exact resumption is required, not merely crossing a weekend.
        return boundary_from_rule(before, rule["next_offset"], calendar["timezone"]) == after
    except ValueError:
        return False


def audit_gaps(asset, timeframe, frames, calendar):
    frame = frames[timeframe]
    duration = DURATIONS[timeframe]
    gaps = []
    for end, delta in frame.index.to_series().diff().items():
        if pd.isna(delta) or delta <= duration:
            continue
        start = end - delta
        if scheduled_gap(calendar, timeframe, start, end):
            continue
        # A bar in a finer timeframe entirely after the earlier native candle
        # would end is independent evidence of activity inside the hole.
        # Even continuous markets can have outages, and old timestamps can shift
        # across DST. A gap alone is uncertainty, not proof of missing candles.
        confirmed = False
        for tf, other in frames.items():
            # Daily/weekly native candles can own a holiday tail across more
            # than their nominal duration. Activity alone cannot prove a hole.
            if timeframe in {"1D", "1W"}:
                break
            if DURATIONS[tf] >= duration or other.empty:
                continue
            pos = other.index.searchsorted(start + duration)
            if pos < len(other) and other.index[pos] < end:
                confirmed = True
                break
        # For hourly gaps, a coarser bar OPENING inside the hole also confirms
        # activity, but do not count a coarser candle overlapping its boundaries.
        if timeframe == "1h":
            # Daily source timestamps can label a trading date rather than a
            # physical opening (notably MOEX holiday/weekend ownership).
            for tf in ("4h",):
                other = frames[tf]
                pos = other.index.searchsorted(start + duration)
                if pos < len(other) and other.index[pos] + DURATIONS[tf] <= end:
                    confirmed = True
                    break
        gaps.append({"after": start.isoformat(), "before": end.isoformat(),
                     "status": "incomplete" if confirmed else "uncertain"})
    return gaps


def window_coverage(source, cutoff, start=None, *, count=None, sessions=None):
    context = source.attrs.get("history_coverage", {})
    if not context:
        return {"status": "uncertain", "unresolved_gaps": []}
    first = source.index.min() if not source.empty else None
    start = pd.Timestamp(start) if start is not None else first
    outside = first is None or cutoff < first or (start is not None and start < first)
    opened = source.loc[source.index <= cutoff]
    if count is not None and len(opened) < count:
        outside = True
    if sessions is not None:
        from .windows import sessions_covered
        outside = outside or sessions_covered(opened) < sessions
    gaps = [g for g in context.get("unresolved_gaps", [])
            if pd.Timestamp(g["after"]) < cutoff
            and (start is None or pd.Timestamp(g["before"]) > start)]
    raw_last = context.get("raw_last")
    timeframe = context.get("timeframe")
    if raw_last and timeframe:
        raw_last = pd.Timestamp(raw_last)
        nominal_end = raw_last + DURATIONS[timeframe]
        resume = context.get("next_session_open")
        if cutoff > nominal_end and not (resume and cutoff <= pd.Timestamp(resume)):
            confirmed = any(pd.Timestamp(t) <= cutoff for t in context.get("tail_evidence", []))
            gaps.append({"after": raw_last.isoformat(), "before": cutoff.isoformat(),
                         "status": "incomplete" if confirmed else "uncertain", "terminal": True,
                         "repair_needed": confirmed or cutoff - nominal_end >= DURATIONS[timeframe]})
    status = "incomplete" if outside or any(g["status"] == "incomplete" for g in gaps) else (
        "uncertain" if gaps else "no_known_gaps")
    result = {"status": status, "unresolved_gaps": gaps, "outside_available_history": outside,
              "available_from": first.isoformat() if first is not None else None,
              "available_to": source.index.max().isoformat() if first is not None else None}
    if context.get("refresh_error"):
        result["refresh_error"] = context["refresh_error"]
        if status == "no_known_gaps":
            result["status"] = "uncertain"
    return result
