from __future__ import annotations

import pandas as pd

from .config import Settings
from .provider import split_asset
from .calendar import expected_boundary


def finalization_delay(settings: Settings, asset: str) -> pd.Timedelta:
    symbol, exchange = split_asset(asset)
    overrides = settings.finalization_delay_by_exchange or {}
    minutes = overrides.get(exchange, settings.finalization_delay_minutes)
    minutes = (settings.finalization_delay_by_asset or {}).get(f"{exchange}:{symbol}", minutes)
    return pd.Timedelta(minutes=int(minutes))


def finality_flags(
    opened: pd.DataFrame,
    duration: pd.Timedelta,
    evaluated_at: pd.Timestamp,
    delay: pd.Timedelta,
) -> pd.Series:
    return completion_details(opened, duration, evaluated_at, delay)["is_bar_complete"]


def completion_details(opened, duration, evaluated_at, delay):
    context = opened.attrs.get("completion_context", {})
    raw_opens = pd.DatetimeIndex(pd.to_datetime(context.get("raw_opens", list(opened.index)), utc=True, format="mixed"))
    raw_opens = raw_opens[raw_opens <= evaluated_at].sort_values()
    calendar = context.get("calendar")
    receipts = context.get("receipts", {})
    tf = context.get("timeframe", {pd.Timedelta(hours=1): "1h", pd.Timedelta(hours=4): "4h",
                                  pd.Timedelta(days=1): "1D", pd.Timedelta(days=7): "1W"}.get(duration))
    records = []
    # pandas propagates/deep-copies attrs into each Series produced by iterrows.
    # Receipt maps and raw timestamp lists belong to the evaluation, not each row.
    rows = opened.copy(deep=False)
    rows.attrs = {}
    for index, row in rows.iterrows():
        raw = pd.Timestamp(row.get("source_timestamp", index))
        flag, reason, boundary = False, "awaiting_successor", None
        if raw <= evaluated_at and raw_opens.searchsorted(raw, side="right") < len(raw_opens):
            flag, reason = True, "successor_received"
        elif raw <= evaluated_at:
            boundary = expected_boundary(calendar, tf, raw, evaluated_at)
            if boundary is None:
                reason = "awaiting_successor" if calendar and calendar.get("status") in {"ready", "no_confirmed_breaks"} else "calendar_unavailable"
            elif evaluated_at < boundary + delay:
                reason = "awaiting_boundary_delay"
            else:
                receipt = receipts.get(raw.isoformat(), {})
                fingerprint = [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
                started = receipt.get("request_started_at")
                if started and pd.Timestamp(started) >= boundary + delay and receipt.get("ohlcv") == fingerprint:
                    # The cutoff bounds the market period, not transport arrival.
                    # A response to a live request necessarily arrives after its
                    # requested_at. Stored OHLCV is reconstructed history, not an
                    # immutable as-received historical feed.
                    flag = True
                    reason = "period_boundary_delay" if tf in {"1D", "1W"} else "session_boundary_delay"
                else:
                    reason = "awaiting_fresh_data"
        records.append({"is_bar_complete": flag, "completion_reason": reason,
                        "completion_boundary": boundary.isoformat() if boundary is not None else None})
    return pd.DataFrame(records, index=opened.index,
                        columns=["is_bar_complete", "completion_reason", "completion_boundary"]).astype({"is_bar_complete": bool})


def calendar_metadata(source):
    calendar = source.attrs.get("completion_context", {}).get("calendar", {}) or {}
    result = {key: calendar.get(key) for key in (
        "version", "built_at", "timezone", "status", "eligible_sessions",
        "training_from", "training_through", "suspended_rules")}
    result["verified_rules_by_timeframe"] = {
        tf: sum(key.startswith(tf + "|") and key not in calendar.get("suspended_rules", {})
                for key in calendar.get("rules", {})) for tf in ("1h", "4h", "1D", "1W")}
    return result
