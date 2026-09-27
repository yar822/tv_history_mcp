"""Run from repository root. Read live calendars; write only to explicit output.

With --populate, also persist calendar metadata through the production manager.
No synchronizer/server is constructed, so this cannot refresh price caches.
"""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd

from tv_history.config import load_settings
from tv_history.provider import TvDatafeedProvider
from tv_history.rollovers import RolloverCalendars
from tv_history.storage import CsvStorage


ASSETS = ("RUS:SI1!", "RUS:MX1!", "COMEX:GC1!", "ICEEUR:BRN1!", "CME_MINI:NQ1!")


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for asset in ASSETS for p in CsvStorage(replace(load_settings(), data_root=root)).path_for(asset).parent.glob("*.csv")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--populate", action="store_true")
    parser.add_argument("--minimum-future", type=int, default=0)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cfg = load_settings()
    before = hashes(cfg.data_root)
    reference = json.loads(args.reference.read_text(encoding="utf-8"))["events"]
    provider = TvDatafeedProvider(cfg)
    test_store = CsvStorage(replace(cfg, data_root=args.output / "cache"))
    collector = RolloverCalendars(provider, test_store)
    results = []
    for asset in ASSETS:
        collector.refresh(asset, pd.Timestamp.now(tz="UTC"))
        document = test_store.read_rollovers(asset)
        expected = [e for e in reference if e["asset"] == asset]
        matched = sum(any((e["from"], e["to"], pd.Timestamp(e["scheduled_at_utc"])) ==
                          (r["old_contract"], r["new_contract"], pd.Timestamp(r["first_new_bar_utc"]))
                          for e in document.get("events", [])) for r in expected)
        future = [e for e in document.get("events", []) if pd.Timestamp(e["scheduled_at_utc"]) > pd.Timestamp(document["checked_at_utc"])] if document.get("checked_at_utc") else []
        result = {"asset": asset, "error": document.get("last_error"),
                  "event_count": len(document.get("events", [])), "expected_historical": len(expected),
                  "matched_historical": matched, "upcoming": future,
                  "local_timestamps_valid": all(e.get("scheduled_at_local") == pd.Timestamp(e["scheduled_at_utc"]).tz_convert(document["timezone"]).isoformat() for e in document.get("events", []))}
        results.append(result)
        print(json.dumps(result), flush=True)
    # Future publication varies by asset/horizon. Absence is evidence to report,
    # not a reason to invent a schedule or discard a valid historical calendar.
    success = all(not r["error"] and r["matched_historical"] == r["expected_historical"]
                  and r["local_timestamps_valid"] and len(r["upcoming"]) >= args.minimum_future for r in results)
    if args.populate and success:
        store = CsvStorage(cfg)
        for asset in ASSETS:
            # Same merge policy as the manager; no second network request.
            from tv_history.rollovers import merge_snapshot
            snapshot = test_store.read_rollovers(asset)
            merged = merge_snapshot(store.read_rollovers(asset), snapshot, pd.Timestamp(snapshot["checked_at_utc"]))
            store.save_rollovers(asset, merged)
    unchanged = before == hashes(cfg.data_root)
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "results": results,
              "passed": bool(success and unchanged), "populated": bool(args.populate and success),
              "raw_csv_hashes_unchanged": unchanged, "raw_csv_count": len(before),
              "validation": "Live provider and storage checks; not an end-to-end MCP run"}
    (args.output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
