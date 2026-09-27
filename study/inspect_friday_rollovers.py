"""Inspect cached Friday rollover events and their next-week raw opening."""
import argparse
import json
from pathlib import Path

import pandas as pd

from tv_history.trading_weeks import hourly_trading_week


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    now = pd.Timestamp.now(tz="UTC")
    results = []
    for exchange, symbol in [("RUS", "SI1"), ("RUS", "MX1"), ("COMEX", "GC1"),
                             ("ICEEUR", "BRN1"), ("CME_MINI", "NQ1")]:
        folder = next(Path("data", exchange).glob(symbol + "_*"))
        schedule = json.loads((folder / "rollovers.json").read_text())
        calendar = json.loads((folder / "calendar.json").read_text())
        prices = pd.read_csv(folder / "1h.csv", index_col=0)
        prices.index = pd.to_datetime(prices.index, utc=True)
        events = []
        for event in schedule["events"]:
            stamp = pd.Timestamp(event["scheduled_at_utc"])
            local = stamp.tz_convert(schedule["timezone"])
            trading_friday = pd.Timestamp(event["trading_date"]).weekday() == 4
            local_friday = local.weekday() == 4
            if not trading_friday and not local_friday:
                continue
            detail = {**event, "future": stamp > now, "trading_date_is_friday": trading_friday,
                      "local_bar_is_friday": local_friday, "cached_hourly_bar": stamp in prices.index}
            week = hourly_trading_week(calendar, stamp)
            detail["week"] = week
            if week and stamp in prices.index and event.get("timestamp_basis") == "1h":
                pos = prices.index.get_loc(stamp)
                tail = prices.loc[(prices.index >= stamp) & (prices.index < pd.Timestamp(week["end_utc"]))]
                next_bars = prices.loc[prices.index >= pd.Timestamp(week["end_utc"])]
                if pos > 0 and not tail.empty and not next_bars.empty:
                    offset = float(prices.iloc[pos - 1].close - prices.iloc[pos].open)
                    raw_gap = float(next_bars.iloc[0].open - tail.iloc[-1].close)
                    detail["example"] = {"offset": round(offset, 8),
                        "last_week_bar": tail.index[-1].isoformat(),
                        "next_week_bar": next_bars.index[0].isoformat(),
                        "raw_weekend_gap": round(raw_gap, 8),
                        "gap_after_removing_offset": round(raw_gap - offset, 8)}
            events.append(detail)
        results.append({"asset": f"{exchange}:{symbol}!", "events_checked": len(schedule["events"]),
                        "friday_events": events})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
