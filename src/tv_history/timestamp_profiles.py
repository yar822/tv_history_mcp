"""Explicit session profiles with operator-selected standard-date fallback.

Raw storage is never modified. Verification limits are exposed in metadata.
"""
import math
import pandas as pd
from .trading_dates import normalize_daily, normalize_weekly
from .market_rules import LEGACY_PROFILE_TIMEZONES


# Local-time half-open ranges: [start hour, midnight). DST comes from the zone.
SESSION_PROFILES = {
    "moex_futures": {"evening_start": 18,
                     "tail_mode": "moex_daytime", "require_tail_volume": True},
    "cme_overnight": {"evening_start": 16,
                      "tail_mode": "overnight", "require_tail_volume": False},
    "ice_brent": {"evening_start": 22,
                  "tail_mode": "daytime_or_overnight", "require_tail_volume": False},
    "utc_calendar": {"evening_start": None,
                     "tail_mode": "disabled", "require_tail_volume": True},
}


def profile_date(opened, profile, timezone=None):
    if profile == "utc_calendar":
        if opened != opened.normalize():
            raise ValueError(f"Non-midnight UTC calendar daily opening: {opened}")
        return opened.normalize()
    if profile not in SESSION_PROFILES:
        raise ValueError(f"Unknown timestamp profile: {profile}")
    zone = timezone or LEGACY_PROFILE_TIMEZONES[profile]
    evening_start = SESSION_PROFILES[profile]["evening_start"]
    local = opened.tz_convert(zone)
    day = pd.Timestamp(local.date(), tz="UTC")
    # Preserve MOEX's separate observed additional-session template.
    additional = profile == "moex_futures" and opened.hour == 6
    if local.hour >= evening_start or additional:
        day += pd.Timedelta(days=1)
        while day.weekday() >= 5:
            day += pd.Timedelta(days=1)
    return day


def served_profile(source, daily, four, hourly, cutoff, timeframe, profile, timezone=None):
    policy = SESSION_PROFILES[profile]
    ending_timezone = timezone or LEGACY_PROFILE_TIMEZONES[profile]
    rejected = []
    def rule(opened):
        try:
            return profile_date(opened, profile, ending_timezone)
        except ValueError:
            # Placeholder solely for running verification; never served as verified.
            rejected.append(opened.isoformat().replace("+00:00", "Z"))
            return opened.normalize()
    mapped = normalize_daily(daily, four, cutoff, hourly, date_rule=rule, ending_timezone=ending_timezone, holiday_policy=policy)
    verified = {}
    for row in mapped.itertuples(index=False):
        raw = row.source_timestamp
        known = row.timestamp_basis == "four_hour_confirmed"
        if profile == "utc_calendar":
            known = raw not in rejected
        verified[raw] = known and not row.timestamp_ambiguous
    if timeframe == "1D":
        view = mapped
        accepted = [verified[raw] for raw in view.source_timestamp] if not view.empty else []
    else:
        view = normalize_weekly(source, daily, four, cutoff, hourly, date_rule=rule,
                                ending_timezone=ending_timezone, holiday_policy=policy,
                                mapped_daily=mapped)
        accepted = []
        raw_daily_times = (pd.DatetimeIndex(pd.to_datetime(mapped.source_timestamp, utc=True))
                           if not mapped.empty else pd.DatetimeIndex([], tz="UTC"))
        for label, row in zip(view.index, view.itertuples(index=False)):
            raw = row.source_timestamp
            start = pd.Timestamp(raw)
            position = source.index.get_loc(start)
            end = source.index[position + 1] if position + 1 < len(source) else None
            part = mapped.iloc[raw_daily_times.searchsorted(start):raw_daily_times.searchsorted(end)] if end is not None else mapped.iloc[:0]
            ok = verified.get(raw, False) and not row.timestamp_ambiguous and not part.empty
            if ok:
                ok = all(verified[raw] for raw in part.source_timestamp)
                ok = ok and all(label <= d < label + pd.Timedelta(days=7) for d in part.index)
                ok = ok and all(math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-8) for a,b in (
                    (part.iloc[0].open, row.open), (part.high.max(), row.high), (part.low.min(), row.low)))
            accepted.append(ok)
    if view.empty:
        view = view.assign(source_timestamp=pd.Series(dtype=str), timestamp_ambiguous=pd.Series(dtype=bool))
    unverified = view.loc[[not x for x in accepted]]
    output = view.copy()
    output.attrs["timestamp_normalization"] = {
        "convention": "verified_trading_labels_v2", "profile": profile,
        "calendar_used": False, "raw_storage_preserved": True,
        "unresolved_policy": "standard_session_fallback", "intraday_source": "local_cache_only",
        "unverified_rows": len(unverified),
        "unverified_source_timestamps": unverified.source_timestamp.tolist(),
        "ambiguous_rows": int(view.timestamp_ambiguous.sum()),
        "unrecognized_source_timestamps": sorted(set(rejected)),
    }
    return output
