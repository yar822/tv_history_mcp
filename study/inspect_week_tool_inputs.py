"""Read saved calendars/hourly timestamps; write a tool-planning evidence report."""
import argparse
import json
from pathlib import Path

import pandas as pd

from tv_history.trading_weeks import trading_week_bounds


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for path in sorted(args.data.glob("*/*/calendar.json")):
        calendar = json.loads(path.read_text(encoding="utf-8"))
        rule = calendar.get("trading_weeks", {})
        frame = pd.read_csv(path.parent / "1h.csv")
        index = pd.DatetimeIndex(pd.to_datetime(frame["timestamp_utc"], utc=True)).sort_values()
        item = {"asset": calendar["asset"], "rule": rule,
                "calendar_status": calendar["status"],
                "session_weekdays": list(calendar.get("sessions", {})),
                "cached_bars": len(index), "cache_first": index[0].isoformat(),
                "cache_last": index[-1].isoformat()}
        if rule.get("status") == "ready":
            groups = {}
            for stamp in index.tz_convert(rule["timezone"]):
                day = stamp.tz_localize(None).normalize()
                monday = day - pd.Timedelta(days=day.weekday())
                if rule["weekend_belongs_to_next_week"] and day.weekday() >= 5:
                    monday += pd.Timedelta(days=7)
                groups.setdefault(monday, []).append(stamp)
            weeks = []
            for monday in sorted(groups)[-5:]:
                stamps = groups[monday]
                bounds = trading_week_bounds(calendar, monday)
                weeks.append({"week": str(monday.date()), "bars": len(stamps),
                              "observed_first_local": stamps[0].isoformat(),
                              "observed_last_local": stamps[-1].isoformat(),
                              "matches_expected_first": stamps[0] == pd.Timestamp(bounds["start_utc"]),
                              "matches_expected_last": stamps[-1] == pd.Timestamp(bounds["end_utc"]) - pd.Timedelta(hours=1)})
            item["recent_observed_weeks"] = weeks
            item["example_week"] = trading_week_bounds(calendar, "2026-09-28")
        results.append(item)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"validation": "Read-only local cache inspection; no provider or live MCP calls. Observed edge weeks may be partial.",
                                       "assets": results}, indent=2) + "\n", encoding="utf-8")
    for item in results:
        print(json.dumps(item))


if __name__ == "__main__":
    main()
