"""Inspect saved rollover/week boundaries without fetching or modifying prices."""
import argparse
import json
from pathlib import Path

import pandas as pd

from tv_history.calendar import expected_boundary
from tv_history.metadata import expand_calendar


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for exchange, symbol in [("RUS", "SI1"), ("RUS", "MX1"), ("COMEX", "GC1"),
                             ("ICEEUR", "BRN1"), ("CME_MINI", "NQ1")]:
        folder = next(Path("data", exchange).glob(symbol + "_*"))
        document = json.loads((folder / "rollovers.json").read_text())
        calendar = expand_calendar(json.loads((folder / "calendar.json").read_text()))
        def prices(tf):
            frame = pd.read_csv(folder / (tf + ".csv"), index_col=0)
            frame.index = pd.to_datetime(frame.index, utc=True)
            return frame.sort_index()
        hourly, weekly = prices("1h"), prices("1W")
        cases = []
        for event in document["events"]:
            t = pd.Timestamp(event["scheduled_at_utc"])
            if event.get("timestamp_basis") != "1h" or not hourly.index.min() <= t <= hourly.index.max():
                continue
            wi = weekly.index.searchsorted(t, side="right") - 1
            if wi < 0:
                continue
            start = weekly.index[wi]
            successor = weekly.index[wi + 1] if wi + 1 < len(weekly) else None
            segment = hourly.loc[(hourly.index >= start) & (hourly.index < successor)] if successor is not None else hourly.loc[hourly.index >= start]
            position = hourly.index.searchsorted(t)
            prior = hourly.index[position - 1] if position else None
            cases.append({"rollover_utc": t.isoformat(), "trading_date": event["trading_date"],
                          "exact_hourly_bar_present": t in hourly.index,
                          "previous_hourly_bar": prior.isoformat() if prior is not None else None,
                          "week_open_utc": start.isoformat(),
                          "next_week_open_utc": successor.isoformat() if successor is not None else None,
                          "last_available_hourly_bar_in_week": segment.index[-1].isoformat() if len(segment) else None,
                          "at_week_open": t == start})
        latest = weekly.index[-1]
        predicted = expected_boundary(calendar, "1W", latest, pd.Timestamp.now(tz="UTC"))
        results.append({"asset": f"{exchange}:{symbol}!", "calendar_status": calendar["status"],
                        "inspection_note": "Raw weekly timestamps are candidate boundaries only, not validated trading-week ownership. An exact hourly timestamp match does not independently confirm the contract switch. Last available bars do not prove a completed week.",
                        "timezone": calendar["timezone"],
                        "weekly_rules": {k: v for k, v in calendar.get("rules", {}).items() if k.startswith("1W|")},
                        "latest_week_open": latest.isoformat(),
                        "latest_week_predicted_close": predicted.isoformat() if predicted is not None else None,
                        "hourly_rollovers_checked": len(cases),
                        "missing_exact_bars": sum(not c["exact_hourly_bar_present"] for c in cases),
                        "latest_cases": cases[-2:]})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
