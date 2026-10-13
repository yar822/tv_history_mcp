"""Read-only recurring session windows from saved hourly calendar evidence."""
from datetime import date as CivilDate, datetime, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import error_response
from .metadata import DAYS
from .provider import split_asset
from .storage import read_json
from .trading_weeks import VERSION


DAY = 86400
WEEK = 7 * DAY
NOTES = [
    "Expected recurring schedule; holidays and exceptional closures are unknown.",
    "Open is inclusive; close is exclusive. Weekly windows can contain daily breaks.",
    "UTC times use the current exchange offset and change with daylight saving; "
    "request again after an offset change.",
]


def _point(day, time):
    hour, minute = map(int, time.split(":"))
    if type(day) is not int or not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError("Invalid saved session boundary")
    return day * DAY + hour * 3600 + minute * 60


def _boundary(seconds, utc=False):
    day, clock = divmod(seconds, DAY)
    hour, clock = divmod(clock, 3600)
    minute, second = divmod(clock, 60)
    return {"day": DAYS[day % 7],
            "time": f"{hour:02d}:{minute:02d}:{second:02d}" + ("Z" if utc else "")}


def _window(start, end, offset):
    if not 0 < end - start <= WEEK:
        raise ValueError("Invalid saved session span")
    return {"open": _boundary(start - offset, True),
            "close": _boundary(end - offset, True),
            "local": {"open": _boundary(start), "close": _boundary(end)}}


def _utc(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Calendar evidence timestamp must have a timezone")
    return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class TradingSessionService:
    def __init__(self, storage, clock=None):
        self.storage = storage
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def get_sessions(self, asset, session="week", date=None):
        try:
            symbol, exchange = split_asset(asset)
            if session not in ("week", "daily"):
                raise ValueError("session must be week or daily")
            if date is not None:
                if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                    raise ValueError("date must be YYYY-MM-DD")
                CivilDate.fromisoformat(date)
            asset = f"{exchange}:{symbol}"
            path = self.storage.path_for(asset).parent / "calendar.json"
        except (TypeError, ValueError) as exc:
            return error_response("INVALID_PARAMETER", str(exc))

        result = {"asset": asset, "session": session, "status": "unavailable",
                  "timezone": "UTC", "sessions": [], "notes": list(NOTES)}

        def unavailable(reason):
            result["reason"] = reason
            return result

        try:
            calendar = read_json(path, {})
            if not calendar:
                return unavailable("calendar_unavailable")
            if not isinstance(calendar, dict):
                return unavailable("invalid_calendar")
            if date is not None:
                from .dated_trading_session import dated_sessions
                return dated_sessions(self.storage, asset, session, date, calendar, self.clock())
            if session == "week":
                evidence = calendar.get("trading_weeks", {})
                if evidence.get("schema_version") != VERSION:
                    return unavailable("unsupported_week_rule")
                if evidence.get("status") != "ready":
                    return unavailable(evidence.get("reason", "week_rule_unavailable"))
            else:
                evidence = calendar
                if calendar.get("schema_version") != 2:
                    return unavailable("unsupported_calendar_schema")

            zone = evidence.get("timezone")
            if not zone:
                return unavailable("no_timezone")
            result["local_timezone"] = zone
            collected_at = self.clock().astimezone(timezone.utc)
            offset = int(collected_at.astimezone(ZoneInfo(zone)).utcoffset().total_seconds())
            hours, minutes = divmod(abs(offset) // 60, 60)
            metadata = {"source": "cached_1h_timestamps",
                        "utc_basis": "current_exchange_offset",
                        "utc_offset_applied": f"{'+' if offset >= 0 else '-'}{hours:02d}:{minutes:02d}",
                        "utc_offset_collected_at_utc": collected_at.isoformat().replace("+00:00", "Z"),
                        "close_basis": "last_hourly_open_plus_1h"}
            windows = []
            if session == "week":
                start = _point(evidence["open"]["day_offset"], evidence["open"]["time"])
                end = _point(evidence["close"]["day_offset"], evidence["close"]["time"])
                windows.append(_window(start, end, offset))
                metadata["method"] = "most_frequent_weekly_boundary_pair"
                for key in ("matching_weeks", "weeks_examined"):
                    if key in evidence:
                        metadata[key] = evidence[key]
                timestamps = {"built_at_utc": "built_at_utc", "training_through_utc": "training_through_utc"}
                status = "recurring_rule"
                if end - start == WEEK:
                    result["notes"].append("Equal weekly opening and closing weekdays/times mean a full seven-day window.")
            else:
                if calendar.get("status") != "ready" or not calendar.get("sessions"):
                    return unavailable("no_learned_daily_sessions")
                spans = []
                for day, entries in calendar["sessions"].items():
                    weekday = DAYS.index(day)
                    for entry in entries:
                        start = _point(weekday + entry.get("start_day_offset", 0), entry["start"])
                        end = _point(weekday + entry.get("end_day_offset", 0), entry["end"])
                        spans.append((start, end))
                if not spans:
                    return unavailable("no_learned_daily_sessions")
                windows = [_window(start, end, offset) for start, end in
                           sorted(spans, key=lambda pair: (pair[0] - offset) % WEEK)]
                metadata.update(method="recurring_contiguous_hourly_runs", coverage="observed_templates_only")
                if "eligible_sessions" in calendar:
                    metadata["eligible_sessions"] = calendar["eligible_sessions"]
                timestamps = {"built_at_utc": "built_at", "training_through_utc": "training_through"}
                result["notes"].append("Missing weekdays are unknown, not confirmed closures.")
                status = "partial"
            for target, source in timestamps.items():
                if evidence.get(source):
                    metadata[target] = _utc(evidence[source])
            result.update(status=status, sessions=windows, metadata=metadata)
            return result
        except ZoneInfoNotFoundError:
            return unavailable("invalid_timezone")
        except OSError:
            return unavailable("calendar_unreadable")
        except (ValueError, TypeError, KeyError, AttributeError):
            return unavailable("invalid_calendar")
