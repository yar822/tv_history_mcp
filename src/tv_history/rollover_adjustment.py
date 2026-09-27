"""Request-scoped hourly price views; source prices and completion stay raw."""
from decimal import Decimal
import re
from threading import RLock

import pandas as pd

from .trading_weeks import hourly_trading_week, trading_week_bounds

OHLC = ["open", "high", "low", "close"]


class RolloverParameterError(ValueError):
    pass


def validate_rollover_parameters(enabled, start=None, end_week=None):
    if not isinstance(enabled, bool):
        raise RolloverParameterError("rollover must be true or false")
    if not enabled:
        return None, None
    parsed = None
    if start is not None:
        try:
            if not isinstance(start, str):
                raise ValueError()
            parsed = pd.Timestamp(start)
            if pd.isna(parsed) or parsed.tzinfo is None:
                raise ValueError()
            parsed = parsed.tz_convert("UTC")
        except (ValueError, TypeError):
            raise RolloverParameterError("rollover_start must be an ISO-8601 timestamp with a timezone") from None
    if end_week is not None:
        try:
            if not isinstance(end_week, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end_week):
                raise ValueError()
            if pd.Timestamp(end_week).weekday() != 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise RolloverParameterError("rollover_end must be a Monday week label in YYYY-MM-DD format") from None
    return parsed, end_week


def rollover_view(synchronizer, source, selected, asset, timeframe, cutoff, enabled,
                  rollover_start=None, rollover_end=None):
    start, end_week = validate_rollover_parameters(enabled, rollover_start, rollover_end)
    metadata = {"enabled": enabled, "applied": False, "events": [],
                "rollover_start": rollover_start, "rollover_end": rollover_end}
    if not enabled or timeframe != "1h" or not asset.split(":")[-1].endswith("1!"):
        metadata["status"] = "disabled" if not enabled else "not_applicable"
        return selected, metadata
    engine = getattr(synchronizer, "rollover_adjustments", None)
    if engine is None:
        return selected, {**metadata, "status": "metadata_unavailable"}
    calendar = source.attrs.get("completion_context", {}).get("calendar") or getattr(synchronizer, "calendars", {}).get(asset, {})
    return engine.apply(asset, source, selected, calendar, cutoff, start, end_week)


def _saved_gap(record):
    """Migrate old cumulative records using their independent, frozen raw gap."""
    names = ("from", "to", "applied_from_utc", "gap_offset", "previous_bar_utc",
             "previous_raw_close", "rollover_raw_open", "recorded_at_utc", "between_weeks")
    return {k: record[k] for k in names if k in record}


class RolloverAdjustments:
    def __init__(self, storage):
        self.storage = storage
        self._lock = RLock()

    def apply(self, asset, source, selected, calendar, cutoff, start=None, end_week=None):
        explicit = start is not None or end_week is not None
        metadata = {"enabled": True, "applied": False, "status": "no_rollover", "events": [],
                    "mode": "episode" if explicit else "rollover_week",
                    "rollover_start": start.isoformat() if start is not None else None,
                    "rollover_end": end_week}
        bounds = trading_week_bounds(calendar or {}, end_week) if end_week else None
        if end_week and bounds is None:
            raise RolloverParameterError("rollover_end cannot be resolved: trading-week calendar unavailable")
        end = pd.Timestamp(bounds["end_utc"]) if bounds else None
        if bounds and start is None:
            start = pd.Timestamp(bounds["start_utc"])
        if end is not None and start >= end:
            raise RolloverParameterError("rollover_start must precede the resolved rollover_end close")
        zone = (calendar or {}).get("trading_weeks", {}).get("timezone")
        metadata.update(resolved_start_utc=start.isoformat() if start is not None else None,
                        resolved_end_utc=end.isoformat() if end is not None else None,
                        resolved_start_local=start.tz_convert(zone).isoformat() if start is not None and zone else None,
                        resolved_end_local=bounds["end_local"] if bounds else None,
                        end_basis="specified_week_close" if bounds else "each_rollover_week_close")
        if selected.empty:
            return selected, metadata
        with self._lock:
            try:
                schedule = self.storage.read_rollovers(asset)
                saved = self.storage.read_rollover_adjustments(asset)
                records = {k: _saved_gap(v) for k, v in saved.get("events", {}).items()}
                if not schedule:
                    return selected, {**metadata, "status": "metadata_unavailable"}
                updates, active = {}, []
                for event in sorted(schedule.get("events", []), key=lambda e: e["scheduled_at_utc"]):
                    stamp = pd.Timestamp(event["scheduled_at_utc"])
                    if (start is not None and stamp < start) or (end is not None and stamp >= end):
                        continue
                    week = hourly_trading_week(calendar or {}, stamp)
                    if week is None:
                        if (end is not None and end > selected.index.min() and stamp <= cutoff) or selected.index.min() <= stamp <= selected.index.max():
                            metadata["events"].append({**event, "status": "unresolved", "reason": "week_unavailable", "offset": None})
                        continue
                    event_end = end if end is not None else pd.Timestamp(week["end_utc"])
                    window_start = start if start is not None else pd.Timestamp(week["start_utc"])
                    if event_end <= selected.index.min() or window_start > selected.index.max():
                        continue
                    key = event["from"] + "->" + event["to"]
                    detail = {"from": event["from"], "to": event["to"], "week": week["week"],
                              "resolved_start_utc": window_start.isoformat(),
                              "applied_from_utc": stamp.isoformat(), "applied_until_utc": event_end.isoformat(),
                              "applied_from_local": stamp.tz_convert(zone).isoformat(),
                              "applied_until_local": event_end.tz_convert(zone).isoformat(),
                              "offset": None, "status": "unresolved", "timestamp_basis": event.get("timestamp_basis")}
                    record = records.get(key)
                    if stamp > cutoff:
                        detail.update(status="scheduled", reason="rollover_not_reached")
                    elif event.get("timestamp_basis") != "1h":
                        detail["reason"] = "awaiting_hourly_rollover_confirmation"
                    elif record and record["applied_from_utc"] != stamp.isoformat():
                        detail["reason"] = "rollover_schedule_changed"
                    else:
                        position = source.index.get_indexer([stamp])[0]
                        previous = pd.Timestamp(record["previous_bar_utc"]) if record else (source.index[position - 1] if position > 0 else None)
                        previous_week = hourly_trading_week(calendar or {}, previous) if previous is not None else None
                        between = (record or {}).get("between_weeks", stamp == pd.Timestamp(week["start_utc"]) or bool(previous_week and previous_week["week"] != week["week"]))
                        if previous is None:
                            detail["reason"] = "rollover_or_previous_bar_missing"
                        elif between and not explicit:
                            detail.update(status="skipped", reason="rollover_between_weeks", offset=0.0)
                        elif previous_week is None and not explicit and record is None:
                            detail["reason"] = "previous_bar_week_unavailable"
                        elif record is None and any(pd.Timestamp(g["after"]) < stamp and pd.Timestamp(g["before"]) > previous
                                for g in source.attrs.get("history_coverage", {}).get("unresolved_gaps", [])
                                if g.get("after") and g.get("before") and g.get("status") == "incomplete"):
                            detail["reason"] = "missing_history_before_rollover"
                        else:
                            gap = Decimal(str(record["gap_offset"])) if record else (
                                Decimal(str(source.iloc[position - 1]["close"])) - Decimal(str(source.iloc[position]["open"])))
                            if not gap.is_finite():
                                detail["reason"] = "invalid_offset_prices"
                            else:
                                if record is None:
                                    record = {"from": event["from"], "to": event["to"], "applied_from_utc": stamp.isoformat(),
                                              "gap_offset": float(gap), "previous_bar_utc": previous.isoformat(),
                                              "between_weeks": between, "recorded_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
                                              "previous_raw_close": float(source.iloc[position - 1]["close"]),
                                              "rollover_raw_open": float(source.iloc[position]["open"])}
                                    updates[key] = record
                                cumulative = gap + sum((Decimal(str(a["gap_offset"])) for a in active
                                    if pd.Timestamp(a["applied_until_utc"]) > stamp), Decimal(0))
                                detail.update(record, status="applied", offset=float(cumulative),
                                              provenance="frozen_raw_contract_gap")
                                active.append(detail)
                    metadata["events"].append(detail)
                if updates or (records and saved.get("schema_version") != 2):
                    records.update(updates)
                    self.storage.save_rollover_adjustments(asset, {"schema_version": 2, "events": records})
                result = selected.copy()
                offsets = pd.Series(0.0, index=result.index)
                for detail in active:
                    mask = (result.index >= pd.Timestamp(detail["applied_from_utc"])) & (result.index < pd.Timestamp(detail["applied_until_utc"]))
                    offsets.loc[mask] += detail["gap_offset"]
                    detail["bars_adjusted"] = int(mask.sum())
                if (offsets != 0).any():
                    result[OHLC] = result[OHLC].add(offsets, axis=0)
                    metadata["applied"] = True
                metadata["status"] = "applied" if metadata["applied"] else "raw"
                metadata["latest_bar_offset"] = float(offsets.iloc[-1])
                return result, metadata
            except (OSError, ValueError, KeyError, TypeError):
                return selected, {**metadata, "applied": False, "status": "adjustment_unavailable", "events": []}
