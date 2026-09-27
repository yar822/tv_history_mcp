"""Build/validate calendar week metadata from local CSVs, without network access."""
import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from tv_history.config import load_settings
from tv_history.calendar import finish
from tv_history.metadata import compact_calendar, expand_calendar
from tv_history.storage import CsvStorage, atomic_json, read_json
from tv_history.trading_weeks import build_trading_weeks, hourly_trading_week


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--populate", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cfg = load_settings(args.config)
    store = CsvStorage(cfg)
    now = pd.Timestamp.now(tz="UTC")
    assets = ["RUS:SI1!", "RUS:MX1!", "COMEX:GC1!", "ICEEUR:BRN1!", "CME_MINI:NQ1!"]
    paths = [store.path_for(a, tf) for a in assets for tf in ("1h", "4h", "1D", "1W")]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    results, updates = [], []
    for asset in assets:
        frames = {tf: store.read(asset, tf) for tf in ("1h", "4h", "1D", "1W")}
        path = store.path_for(asset).parent / "calendar.json"
        original = read_json(path, {})
        calendar = expand_calendar(original)
        calendar["trading_weeks"] = build_trading_weeks(asset, frames, cfg, now)
        assert calendar["trading_weeks"]["status"] == "ready", calendar["trading_weeks"]
        assignments = [hourly_trading_week(calendar, t) for t in frames["1h"].index]
        for entry in assignments:
            if entry is not None:
                assert pd.Timestamp(entry["start_local"]) == pd.Timestamp(entry["start_utc"])
                assert pd.Timestamp(entry["end_local"]) == pd.Timestamp(entry["end_utc"])
        cases = []
        for event in read_json(path.parent / "rollovers.json", {}).get("events", []):
            assignment = hourly_trading_week(calendar, event["scheduled_at_utc"])
            if assignment is not None:
                cases.append({"rollover": event["scheduled_at_utc"], "assignment": assignment})
        result = {"asset": asset, "rule": calendar["trading_weeks"],
                  "assigned_hours": sum(e is not None for e in assignments),
                  "cached_hours": len(frames["1h"]), "rollover_cases": cases}
        results.append(result)
        # Keep original calendars for review/rollback; preserve existing session rules.
        backup = args.output / (asset.replace(":", "_").replace("!", "") + "_before.json")
        if not backup.exists():
            atomic_json(backup, original)
        calendar.pop("version", None)
        updated = compact_calendar(finish(calendar))
        assert expand_calendar(updated)["trading_weeks"] == calendar["trading_weeks"]
        updates.append((path, updated))
        print(json.dumps({k: v for k, v in result.items() if k != "rollover_cases"}), flush=True)
    if args.populate:
        for path, updated in updates:
            atomic_json(path, updated)
    unchanged = all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in hashes.items())
    assert unchanged
    atomic_json(args.output / "report.json", {"assets": results, "raw_csv_count": len(paths),
                "raw_csv_hashes_unchanged": unchanged, "populated": args.populate,
                "validation": "Local cache validation only; no live MCP or provider calls"})


if __name__ == "__main__":
    main()
