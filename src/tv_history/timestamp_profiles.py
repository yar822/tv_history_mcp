"""Explicit session profiles with operator-selected standard-date fallback.

Raw storage is never modified. Verification evidence stays on individual bars.
"""
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
    def rule(opened):
        try:
            return profile_date(opened, profile, ending_timezone)
        except ValueError:
            # Preserve the existing standard-date fallback for unknown openings.
            return opened.normalize()
    mapped = normalize_daily(daily, four, cutoff, hourly, date_rule=rule, ending_timezone=ending_timezone, holiday_policy=policy)
    if timeframe == "1D":
        view = mapped
    else:
        view = normalize_weekly(source, daily, four, cutoff, hourly, date_rule=rule,
                                ending_timezone=ending_timezone, holiday_policy=policy,
                                mapped_daily=mapped)
    if view.empty:
        view = view.assign(source_timestamp=pd.Series(dtype=str), timestamp_ambiguous=pd.Series(dtype=bool))
    return view
