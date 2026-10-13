"""Check updated completion against cached Gold without changing runtime data."""
import json
from pathlib import Path

import pandas as pd

from tv_history.finality import completion_details
from tv_history.metadata import expand_calendar


root = Path("data/COMEX/GC1_-bb4b5e644e")
source = pd.read_csv(root / "1h.csv", index_col="timestamp_utc", parse_dates=True)
calendar = expand_calendar(json.loads((root / "calendar.json").read_text(encoding="utf-8")))
target = source.loc[[pd.Timestamp("2026-09-18T20:00Z")]].copy()
target.attrs["completion_context"] = {
    "timeframe": "1h", "calendar": calendar, "raw_opens": list(source.index)}
checks = []
for cutoff, complete, reason in [
    ("2026-09-18T20:30Z", False, "awaiting_successor"),
    ("2026-09-18T21:00Z", True, "weekly_close_successor_confirmed"),
    ("2026-09-18T21:15Z", True, "weekly_close_successor_confirmed"),
    ("2026-09-21T01:00Z", True, "successor_received"),
]:
    result = completion_details(target, pd.Timedelta(hours=1), pd.Timestamp(cutoff), pd.Timedelta(minutes=15)).iloc[0].to_dict()
    assert result["is_bar_complete"] == complete and result["completion_reason"] == reason
    checks.append({"cutoff": cutoff, **result})
output = Path("data/investigations/gold_weekly_close_fixed_20260928.json")
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({"validation": "local evaluator against cached data; not live MCP", "checks": checks}, indent=2) + "\n", encoding="utf-8")
print(f"All four cached Gold checks passed: {output}")
