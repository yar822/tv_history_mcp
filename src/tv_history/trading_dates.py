"""Calendar-free daily labels for explicitly configured RUS session templates.

Storage remains in source time. These labels are trading dates, not session opens.
The standard rule is an assumption; bracketed 4h evidence can correct it.
"""
from __future__ import annotations

import math

import pandas as pd

from .provider import split_asset
from .config import DEFAULT_RUS_DAILY_TRADING_DATE_ASSETS


CONVENTION = "rus_daily_trading_date_v1"


def uses_trading_dates(
    asset: str,
    timeframe: str,
    configured_assets: tuple[str, ...] = DEFAULT_RUS_DAILY_TRADING_DATE_ASSETS,
) -> bool:
    symbol, exchange = split_asset(asset)
    return timeframe in {"1D", "1W"} and exchange == "RUS" and f"{exchange}:{symbol}" in configured_assets


def standard_date(opened: pd.Timestamp) -> pd.Timestamp:
    """Recognize the observed UTC session templates, without a holiday calendar."""
    if opened.minute or opened.second or opened.microsecond or opened.nanosecond:
        raise ValueError(f"Unrecognized RUS daily session timestamp: {opened}")
    day = opened.normalize()
    if opened.hour in {3, 4, 5}:
        return day
    if opened.hour == 6:
        # Additional sessions may open on a weekday holiday as well as Saturday.
        day += pd.Timedelta(days=1)
    elif opened.hour in {15, 16, 17}:
        day += pd.Timedelta(days=1)
    else:
        raise ValueError(f"Unrecognized RUS daily session timestamp: {opened}")
    while day.weekday() >= 5:
        day += pd.Timedelta(days=1)
    return day


def normalize_daily(
    source: pd.DataFrame,
    four_hour: pd.DataFrame,
    evaluated_at: pd.Timestamp,
    hourly: pd.DataFrame | None = None,
    date_rule=standard_date,
    ending_timezone="Europe/Moscow",
    holiday_policy=None,
) -> pd.DataFrame:
    """Return a normalized view; never alter source or stored 4h evidence.

    A successor daily bar must exist and have opened by wall-clock evaluation
    before 4h data can correct a date. Evidence must bracket both daily opens,
    contain finalized nominal 4h intervals, and reproduce daily O/H/L. Daily
    settlement closes and separately revised volume are intentionally not used.
    Historical requests may use later evidence to identify ownership, but cannot
    receive a bar before its original source open.
    """
    if source.empty:
        return source.copy()
    for frame in (source, four_hour):
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise ValueError("RUS timestamp normalization requires unique sorted source bars")
    if hourly is not None and (hourly.index.has_duplicates or not hourly.index.is_monotonic_increasing):
        raise ValueError("RUS hourly evidence requires unique sorted source bars")
    labels, bases, checks = [], [], []
    observed_at = pd.Timestamp.now(tz="UTC")
    for position, (opened, row) in enumerate(zip(source.index, source.itertuples(index=False))):
        label = date_rule(opened)
        basis = "standard_session_rule"
        following = source.index[position + 1] if position + 1 < len(source) else None
        # A recorded Saturday evening successor proves the preceding session
        # cannot belong to Monday. This also protects older history without 4h.
        if following is not None and label > following.normalize():
            label = following.normalize()
            basis = "next_daily_boundary"
        check = "no_successor_daily_bar"
        if following is not None:
            check = "four_hour_unavailable_or_unbracketed"
            if following <= observed_at and opened in four_hour.index and following in four_hour.index:
                segment = four_hour.iloc[four_hour.index.searchsorted(opened):four_hour.index.searchsorted(following)]
                check = "four_hour_interval_overlaps_successor"
                overlaps = segment.loc[segment.index + pd.Timedelta(hours=4) > following]
                hourly_verified = False
                if not overlaps.empty and hourly is not None:
                    # Only the final bar may be a shortened session-end bar.
                    # Verify its actual OHLC with a complete hourly prefix;
                    # never merely clip the 4h timestamp and trust its prices.
                    hourly_verified = len(overlaps) == 1 and overlaps.index[0] == segment.index[-1]
                    if hourly_verified:
                        start = overlaps.index[0]
                        hours = hourly.iloc[hourly.index.searchsorted(start):hourly.index.searchsorted(following)]
                        expected = pd.date_range(start, following, freq="1h", inclusive="left")
                        hourly_verified = (
                            following in hourly.index
                            and not hours.empty
                            and hours.index.equals(expected)
                            and (hours.index + pd.Timedelta(hours=1) <= following).all()
                        )
                        if hourly_verified:
                            prefix = (hours.iloc[0]["open"], hours["high"].max(), hours["low"].min(), hours.iloc[-1]["close"])
                            hourly_verified = all(math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-8)
                                                  for a, b in zip(prefix, overlaps.iloc[0][["open", "high", "low", "close"]]))
                    if not hourly_verified:
                        check = "session_end_hourly_verification_failed"
                if not segment.empty and (overlaps.empty or hourly_verified):
                    matches = all(math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-8) for a, b in (
                        (segment.iloc[0]["open"], row.open),
                        (segment["high"].max(), row.high),
                        (segment["low"].min(), row.low),
                    ))
                    check = "four_hour_ohl_mismatch"
                    holiday_tail = False
                    if not matches:
                        # Some holiday daily stamps retain the old evening open,
                        # but OHLCV covers only the reopening day's main session.
                        # Inspect only the final contiguous segment after a full
                        # day without bars; never search arbitrary price suffixes.
                        gaps = segment.index[1:] - segment.index[:-1]
                        breaks = [i + 1 for i, gap in enumerate(gaps)
                                  if gap >= pd.Timedelta(hours=28)]
                        if breaks:
                            tail = segment.iloc[breaks[-1]:]
                            contiguous = all(gap == pd.Timedelta(hours=4)
                                             for gap in tail.index[1:] - tail.index[:-1])
                            one_day = len(set(tail.index.tz_convert(ending_timezone).date)) == 1
                            reaches_end = tail.index[-1] + pd.Timedelta(hours=4) >= following
                            mode = (holiday_policy or {}).get("tail_mode", "moex_daytime")
                            volume_required = (holiday_policy or {}).get("require_tail_volume", True)
                            local_start = tail.index[0].tz_convert(ending_timezone)
                            local_end = tail.index[-1].tz_convert(ending_timezone)
                            shape_matches = one_day and tail.index[0].hour in {3, 4, 5, 6}
                            if mode in {"overnight", "daytime_or_overnight"}:
                                next_date = (pd.Timestamp(local_start.date()) + pd.Timedelta(days=1)).date()
                                overnight = (local_start.hour >= holiday_policy["evening_start"]
                                             and local_end.date() == next_date)
                                daytime = mode == "daytime_or_overnight" and local_start.hour < 5 and one_day
                                shape_matches = (overnight or daytime) and following - tail.index[0] <= pd.Timedelta(hours=25)
                            elif mode == "disabled":
                                shape_matches = False
                            if (len(tail) >= 2 and contiguous and shape_matches and reaches_end):
                                matches = all(math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-8)
                                              for a, b in (
                                                  (tail.iloc[0]["open"], row.open),
                                                  (tail["high"].max(), row.high),
                                                  (tail["low"].min(), row.low),
                                              ))
                                volume_matches = math.isclose(float(tail["volume"].sum()), float(row.volume), rel_tol=0, abs_tol=1e-8)
                                matches = matches and (volume_matches or not volume_required)
                                holiday_tail = matches
                    if matches:
                        ending_date = pd.Timestamp(segment.index[-1].tz_convert(ending_timezone).date(), tz="UTC")
                        check = "four_hour_ohl_matches_hourly_session_end" if hourly_verified else "four_hour_ohl_matches"
                        if holiday_tail:
                            check = ("four_hour_holiday_tail_ohlv_matches" if volume_matches else "four_hour_holiday_tail_ohl_matches_volume_differs") + ("_hourly_session_end" if hourly_verified else "")
                        # A bracketed completed session may end on a working
                        # Saturday, earlier than the standard Monday fallback.
                        if opened.normalize() <= ending_date <= following.normalize():
                            label = ending_date
                            basis = "four_hour_confirmed"
        labels.append(label)
        bases.append(basis)
        checks.append(check)
    normalized = source.copy()
    normalized["source_timestamp"] = [stamp.isoformat().replace("+00:00", "Z") for stamp in source.index]
    normalized["timestamp_basis"] = bases
    normalized["timestamp_evidence"] = checks
    normalized.index = pd.DatetimeIndex(labels, name=source.index.name)
    if not normalized.index.is_monotonic_increasing:
        raise ValueError("Ambiguous RUS trading dates: normalization would reorder bars")
    # The operator explicitly chose standard-rule fallback when 4h is absent.
    # Preserve both source rows if that assumption gives them the same label.
    # source_timestamp remains their unique identity; never deduplicate OHLCV.
    normalized["timestamp_ambiguous"] = normalized.index.duplicated(keep=False)
    # A midnight label must not expose a same-day morning bar before it opened.
    normalized = normalized.loc[source.index <= evaluated_at].copy()
    normalized.attrs["timestamp_normalization"] = {
        "convention": CONVENTION,
        "calendar_used": False,
        "raw_storage_preserved": True,
        "four_hour_source": "local_cache_only",
        "session_end_hourly_source": "local_cache_only",
        "standard_dates_are_assumptions": True,
        "ambiguous_rows": int(normalized["timestamp_ambiguous"].sum()),
        "ambiguous_source_timestamps": normalized.loc[
            normalized["timestamp_ambiguous"], "source_timestamp"
        ].tolist(),
        "basis_counts": normalized["timestamp_basis"].value_counts().to_dict(),
        "evidence_counts": normalized["timestamp_evidence"].value_counts().to_dict(),
    }
    return normalized


def normalize_weekly(
    source: pd.DataFrame,
    daily: pd.DataFrame,
    four_hour: pd.DataFrame,
    evaluated_at: pd.Timestamp,
    hourly: pd.DataFrame | None = None,
    date_rule=standard_date,
    ending_timezone="Europe/Moscow",
    holiday_policy=None,
    *,
    mapped_daily: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Label source weeks by the Monday of their opening trading date.

    Match the opening daily bar by original timestamp and open price, then use
    its existing calendar-free/4h mapping. Missing daily evidence falls back to
    the standard rule. Never apply the daily end-date algorithm to a whole week.
    """
    if source.empty:
        return source.copy()
    if source.index.has_duplicates or not source.index.is_monotonic_increasing:
        raise ValueError("RUS timestamp normalization requires unique sorted source bars")
    if mapped_daily is None:
        mapped_daily = normalize_daily(daily, four_hour, evaluated_at, hourly=hourly, date_rule=date_rule, ending_timezone=ending_timezone, holiday_policy=holiday_policy)
    daily_dates = {
        row.source_timestamp: (label, row)
        for label, row in zip(mapped_daily.index, mapped_daily.itertuples(index=False))
    }
    labels, bases, evidence, ambiguous = [], [], [], []
    for opened, row in zip(source.index, source.itertuples(index=False)):
        date = date_rule(opened)
        basis, check, uncertain = "standard_session_rule", "opening_daily_unavailable", False
        match = daily_dates.get(opened.isoformat().replace("+00:00", "Z"))
        if match is not None:
            daily_date, daily_row = match
            check = "opening_daily_open_mismatch"
            if math.isclose(float(row.open), float(daily_row.open), rel_tol=0, abs_tol=1e-8):
                date = daily_date
                basis = "opening_daily_" + daily_row.timestamp_basis
                check = daily_row.timestamp_evidence
                uncertain = bool(daily_row.timestamp_ambiguous)
        labels.append(date - pd.Timedelta(days=date.weekday()))
        bases.append(basis)
        evidence.append(check)
        ambiguous.append(uncertain)
    normalized = source.copy()
    normalized["source_timestamp"] = [stamp.isoformat().replace("+00:00", "Z") for stamp in source.index]
    normalized["timestamp_basis"] = bases
    normalized["timestamp_evidence"] = evidence
    normalized.index = pd.DatetimeIndex(labels, name=source.index.name)
    if not normalized.index.is_monotonic_increasing:
        raise ValueError("Ambiguous RUS trading weeks: normalization would reorder bars")
    normalized["timestamp_ambiguous"] = normalized.index.duplicated(keep=False) | pd.array(ambiguous, dtype=bool)
    normalized = normalized.loc[source.index <= evaluated_at].copy()
    normalized.attrs["timestamp_normalization"] = {
        "convention": "rus_weekly_trading_week_v1",
        "calendar_used": False,
        "raw_storage_preserved": True,
        "daily_and_four_hour_source": "local_cache_only",
        "standard_dates_are_assumptions": True,
        "ambiguous_rows": int(normalized["timestamp_ambiguous"].sum()),
        "ambiguous_source_timestamps": normalized.loc[
            normalized["timestamp_ambiguous"], "source_timestamp"
        ].tolist(),
        "basis_counts": normalized["timestamp_basis"].value_counts().to_dict(),
        "evidence_counts": normalized["timestamp_evidence"].value_counts().to_dict(),
    }
    return normalized
