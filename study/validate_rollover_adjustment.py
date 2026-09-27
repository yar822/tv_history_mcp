"""Validate copied rollover metadata against raw local prices; no provider calls."""
import argparse
from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from tv_history.config import load_settings
from tv_history.metadata import expand_calendar
from tv_history.rollover_adjustment import RolloverAdjustments, rollover_view
from tv_history.storage import CsvStorage, read_json, atomic_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cfg = load_settings("config.yaml")
    original = CsvStorage(cfg)
    copied = CsvStorage(replace(cfg, data_root=args.output.resolve() / "cache"))
    assets = ["RUS:SI1!", "RUS:MX1!", "COMEX:GC1!", "ICEEUR:BRN1!", "CME_MINI:NQ1!"]
    paths = [original.path_for(a, tf) for a in assets for tf in ("1h", "4h", "1D", "1W")]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report = []
    for asset in assets:
        source = original.read(asset, "1h")
        source.attrs = {"completion_context": {"calendar": expand_calendar(read_json(original.path_for(asset).parent / "calendar.json", {}))},
                        "history_coverage": original.read_coverage(asset).get("1h", {})}
        copied.save_rollovers(asset, original.read_rollovers(asset))
        sync = SimpleNamespace(rollover_adjustments=RolloverAdjustments(copied))
        adjusted, metadata = rollover_view(sync, source, source, asset, "1h", source.index[-1], True)
        off, _ = rollover_view(sync, source, source, asset, "1h", source.index[-1], False)
        pd.testing.assert_frame_equal(off, source)
        pd.testing.assert_series_equal(adjusted.volume, source.volume)
        delta = adjusted.close - source.close
        for col in ("open", "high", "low"):
            assert ((adjusted[col] - source[col] - delta).abs() < 1e-8).all()
        allowed = pd.Series(False, index=source.index)
        for event in metadata["events"]:
            if event["status"] == "applied":
                allowed |= (source.index >= pd.Timestamp(event["applied_from_utc"])) & (source.index < pd.Timestamp(event["applied_until_utc"]))
        assert (delta.loc[~allowed] == 0).all()
        for tf in ("4h", "1D", "1W"):
            frame = original.read(asset, tf)
            unchanged, _ = rollover_view(sync, frame, frame, asset, tf, source.index[-1], True)
            pd.testing.assert_frame_equal(unchanged, frame)
        episodes = []
        if asset == "ICEEUR:BRN1!":
            for start, end_week, expected in [
                ("2026-09-18T19:00:00Z", "2026-09-21", 5.25),
                ("2026-09-24T01:00:00Z", "2026-09-28", None),
                ("2026-08-21T19:00:00Z", "2026-08-31", 1.7),
                (None, "2026-08-24", 1.7),
            ]:
                view, episode = rollover_view(sync, source, source, asset, "1h", source.index[-1], True, start, end_week)
                changes = view.close - source.close
                active = [e for e in episode["events"] if e["status"] == "applied"]
                if expected is None:
                    assert not active and (changes == 0).all()
                else:
                    assert len(active) == 1 and abs(active[0]["gap_offset"] - expected) < 1e-8
                    assert (changes != 0).any()
                    assert (changes.loc[source.index >= pd.Timestamp(episode["resolved_end_utc"])] == 0).all()
                episodes.append(episode)
        report.append({"asset": asset, "adjusted_bars": int((delta != 0).sum()), "metadata": metadata, "episodes": episodes})
        print(asset, "adjusted bars", report[-1]["adjusted_bars"], flush=True)
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in hashes.items())
    atomic_json(args.output / "report.json", {"assets": report, "raw_csv_count": len(paths),
                "raw_csv_hashes_unchanged": True, "validation": "Local prices with copied metadata; no live MCP/provider calls"})


if __name__ == "__main__":
    main()
